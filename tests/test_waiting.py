"""The waiting loop, checked where it is allowed to be wrong.

There is no network here and no real time. The fake below is the whole hub, its
clock moves only when the waiter sleeps, and the file runs in milliseconds —
deliberately, because a wait tested against real seconds is a suite that gets
slower every time the default timeout grows.

Four of these are about a collapse rather than a feature, and each has a
direction. A waiter that concluded from the stream would report "your peer said
nothing" (exit 1) during an outage, because `client.stream` returns silently when
its socket dies. A waiter that opened a stream with the last scraps of a deadline
would report "the hub is unreachable" (exit 2) while the hub was healthy. A
waiter that filtered on `kind` or a correlation id would walk past the exchange
this whole cut came from — a peer answered an earlier `tell` with a `tell`,
seconds *before* the `ask` landed; that answer settled the question too, and
carried the lower sequence number. And a waiter with no floor under its
reconnects turns a stream that dies at once into a poll flood on the hub's single
serialized connection.

The filter test is AST-based rather than a substring over the module source. The
module's own docstring says the words the body must not contain, so the naive
version of that test is red against a property that holds.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator

import pytest

from cairn import waiting
from cairn.errors import Unreachable
from cairn.wire import InboxPage, Message

ME = "bench/firmware"
PEER = "compute/analysis"

STREAM_COST = 0.05
"""What one stream attempt charges the fake clock, body or no body.

Never zero. Connecting takes wall time on a real socket, and a fake that says
otherwise makes the unfloored loop in `test_a_stream_that_dies_at_once_does_not_spin`
reconnect forever instead of the 1181 times it measures.
"""


class FakeHub:
    """A stand-in for `HubClient` that records every call and owns the clock.

    `now` reads a mutable float and `sleep` advances that same float, which is
    the pair `wait_for_mail` insists on being given together: fake one without
    the other and the test either spends the deadline in real seconds or never
    terminates at all.
    """

    def __init__(self) -> None:
        """Build a hub with an empty inbox and a stream whose body ends at once."""
        self.calls: list[str] = []
        self.clock = 1000.0
        self.mail: list[Message] = []
        self.chunks_per_stream = 0
        self.heartbeat: float | None = None
        self.stream_cost = STREAM_COST
        self.forbid_stream = False
        self.stream_raises: Exception | None = None
        self.on_poll = None
        self.limits: list[int] = []
        self.stream_timeouts: list[float] = []
        self.polls = 0

    # -- the two injected seams ------------------------------------------------

    def now(self) -> float:
        """Read the fake clock."""
        return self.clock

    def sleep(self, seconds: float) -> None:
        """Spend `seconds` of fake wall time, instantly."""
        self.clock += seconds

    # -- the slice of HubClient the waiter is allowed to touch -----------------

    def inbox(self, agent: str, limit: int = 50) -> InboxPage:
        """Return what is unread, then let the test change the world.

        Pages like the real one: `messages` is capped by `limit` while `unread`
        and `head` are not. A fake that reported the page as the backlog would
        agree with the waiter no matter which of the two it stopped on, and the
        rule that it must stop on the page is exactly what needs holding.
        """
        self.calls.append("inbox")
        self.limits.append(limit)
        self.polls += 1
        unread = list(self.mail)
        if self.on_poll is not None:
            # After the snapshot, so `on_poll(1)` means "this arrived once the
            # first read had already come back empty" — the subscription race.
            self.on_poll(self.polls)
        return InboxPage(
            messages=tuple(unread[:limit]),
            unread=len(unread),
            head=max((m.seq for m in unread), default=0),
        )

    def ack(self, agent: str, seq: int, *, rewind: bool = False) -> int:
        """Record an ack the waiter must never perform."""
        self.calls.append("ack")
        return seq

    def stream(self, agent: str, timeout: float = 60.0) -> Iterator[bytes]:
        """Hand back a bell stream, or fail the test for having asked."""
        self.calls.append("stream")
        self.stream_timeouts.append(timeout)
        if self.forbid_stream:
            msg = "opened a bell stream although the first read had already answered"
            raise AssertionError(msg)
        return self._bells(timeout)

    def _bells(self, timeout: float) -> Iterator[bytes]:
        self.clock += self.stream_cost
        if self.stream_raises is not None:
            # A generator, so this lands on the first `next()`, exactly where
            # `client.stream` raises: the connection is attempted lazily.
            raise self.stream_raises
        if self.heartbeat is not None:
            yield from self._keepalives(timeout)
            return
        for _ in range(self.chunks_per_stream):
            yield b": keep-alive\n\n"

    def _keepalives(self, timeout: float) -> Iterator[bytes]:
        """Tick on the hub's interval until the socket timeout beats the next tick.

        The restart is the part worth modelling: `HTTPResponse.read1` applies the
        socket timeout to each read, so every keep-alive hands the next read a
        fresh full budget. That is how a wait ends up outliving its own deadline.
        """
        yield b"event: hello\n\n"  # sent as soon as the subscription exists
        while self.heartbeat is not None and self.heartbeat <= timeout:
            self.clock += self.heartbeat
            yield b": keep-alive\n\n"
        self.clock += timeout


def _message(seq: int, kind: str = "tell", correlation_id: str | None = None) -> Message:
    return Message(
        seq=seq,
        kind=kind,
        sender=PEER,
        recipient=ME,
        body="every failure is above 40 degrees",
        correlation_id=correlation_id,
    )


def _wait(hub, timeout, **kwargs):
    """Run the waiter against `hub` with both seams wired to it. Never call it any other way."""
    return waiting.wait_for_mail(hub, ME, timeout=timeout, now=hub.now, sleep=hub.sleep, **kwargs)


def _mail(hub, timeout, **kwargs):
    """Run the waiter and return only the page, for the assertions about mail."""
    return list(_wait(hub, timeout, **kwargs).messages)


# -- what it must not do -------------------------------------------------------


def test_it_never_blocks_when_the_answer_is_already_there():
    """The ordinary read runs first and unconditionally. A settled question cannot block."""
    hub = FakeHub()
    hub.mail = [_message(41)]
    hub.forbid_stream = True

    assert _mail(hub, 60.0) == hub.mail
    assert hub.calls == ["inbox"]
    assert hub.clock == 1000.0, "it spent time on a question that was already answered"


def test_the_waiter_cannot_learn_what_kind_a_message_is():
    """The three words that would have skipped the live answer, and the import that would allow them.

    AST rather than a substring over `inspect.getsource(waiting)`: the module
    docstring says "never looks at `kind`", so the substring version is red
    against a module that is right.
    """
    body = _wait_for_mail_body()
    for word in ("correlation_id", "kind", "reply"):
        assert word not in body, f"the waiting loop mentions {word!r}; every filter is a way to skip the answer"

    imported = _imported_modules()
    assert not any("events" in name for name in imported), (
        f"waiting imports {imported} — decoding a bell puts kind and correlation id back in scope"
    )


def test_the_waiter_never_acks():
    """Advancing the read position is the caller's act, after it has printed."""
    delivered = FakeHub()
    delivered.mail = [_message(41)]
    assert _mail(delivered, 60.0) == delivered.mail
    assert "ack" not in delivered.calls

    empty = FakeHub()
    assert _mail(empty, 60.0) == []
    assert "ack" not in empty.calls


def test_a_deadline_too_short_to_stream_never_opens_one():
    """Under a second there is no time for a subscription to be worth registering.

    It still stands there for the second. `cli.py` prints the number it was
    asked for, so a `--wait 0.5` that returned in 0.000s put a duration in the
    transcript that had not happened — and the whole reason that line exists is
    that a transcript should distinguish standing still from glancing once.
    """
    for timeout in (0.5, waiting.MIN_STREAM_SECONDS):
        hub = FakeHub()
        assert _mail(hub, timeout) == []
        assert hub.calls == ["inbox", "inbox"], f"a {timeout}s deadline still went to the stream"
        assert hub.clock == pytest.approx(1000.0 + timeout), "it reported a wait it did not spend"


# -- who is allowed to decide ---------------------------------------------------


def test_the_last_word_belongs_to_the_hub_not_the_clock():
    """The deadline ends the loop. It never produces the verdict."""
    quiet = FakeHub()
    assert _mail(quiet, 60.0) == []
    assert quiet.calls[-1] == "inbox", "an expired clock, not the hub, said there was nothing"
    assert quiet.clock == pytest.approx(1060.0), "it gave up before the deadline it was given"

    late = FakeHub()

    def deliver_in_the_last_five_seconds(poll):
        if late.clock >= 1055.0:
            late.mail = [_message(41)]

    # `on_poll` fires after the snapshot, so this lands *between* the last floored
    # poll and the trailing one — the narrowest window there is, and the one where
    # a waiter that let the clock produce the verdict would drop it.
    late.on_poll = deliver_in_the_last_five_seconds
    assert _mail(late, 60.0) == late.mail, "mail that landed as the deadline passed was thrown away"
    assert late.clock == pytest.approx(1060.0), "it returned before the deadline it was given"


def test_a_stream_that_will_not_open_is_silence_not_an_outage():
    """The other direction of the same collapse: a missing event route is not a dead hub.

    `client.stream` raises `Unreachable` on a 404, on a 502, and on a refused
    connection. A hub that answers `/v1/inbox` and not `/v1/events` — one built
    before the bell stream, or one behind an ingress that will not pass
    `text/event-stream` — would otherwise end a 60-second wait in 0.000s with
    exit 2, microseconds after the first read had proved the hub was up. The
    stream is never a verdict, including about itself.
    """
    hub = FakeHub()
    hub.stream_raises = Unreachable("hub returned 404 on the bell stream: no route /v1/events")

    assert _mail(hub, 60.0) == []
    assert hub.calls[-1] == "inbox", "the stream, not the hub, ended the wait"
    assert hub.calls.count("stream") > 1, "it gave up on the stream instead of retrying under the floor"
    assert hub.polls > 2, "a route that 404s must degrade to a five-second poll, not to a sleep"
    assert hub.polls <= 60 / waiting.TICK_FLOOR + 2, "and not to a flood"
    assert hub.clock == pytest.approx(1060.0), "it stopped short of the deadline it was given"


def test_an_unreachable_hub_is_not_an_empty_inbox():
    """Exit 2 must not become exit 1: an outage and a silent peer are opposite facts."""
    hub = FakeHub()
    hub.chunks_per_stream = 1

    def die_on_the_second_read(poll):
        if poll == 2:
            msg = "cannot reach hub at http://hub.invalid: [Errno 111] Connection refused"
            raise Unreachable(msg)

    hub.on_poll = die_on_the_second_read
    with pytest.raises(Unreachable, match="cannot reach hub"):
        _wait(hub, 60.0)


# -- how hard it leans on the hub ------------------------------------------------


def test_a_stream_that_dies_at_once_does_not_spin(monkeypatch):
    """A body that ends immediately and forever — a proxy that strips SSE — must degrade, not flood.

    Measured on the fake, 60-second deadline, stream attempts costing 50 ms: 12
    reconnects and 14 polls with `TICK_FLOOR`, against 1181 reconnects and
    **1183** polls without it — every poll a durable write through the hub's
    single serialized connection. That ratio is the reason the constant exists;
    the floor turns the failure into a five-second poll, which is slower and
    still correct.
    """
    floored = FakeHub()
    floored.chunks_per_stream = 1
    assert _mail(floored, 60.0) == []
    assert floored.polls <= 60 / waiting.TICK_FLOOR + 2

    monkeypatch.setattr(waiting, "TICK_FLOOR", 0.0)
    unfloored = FakeHub()
    unfloored.chunks_per_stream = 1
    assert _mail(unfloored, 60.0) == []
    assert unfloored.polls > 50 * floored.polls, "removing the floor cost nothing — the fake is not reconnecting"


def test_a_wait_ends_on_its_own_deadline_and_not_the_hub_s_next_heartbeat():
    """A keep-alive restarts the socket timeout, so the naive loop rounds the wait up.

    Measured against a live hub before the fix, at the shipped 20-second
    heartbeat: `--wait 25` returned in **40.01s**, and with the heartbeat
    patched to 2.0s, `--wait 3/5/7` returned in 4.02/6.01/8.01 — the first
    heartbeat at or after the deadline, every time. The default 60 is an exact
    multiple of 20 and lands on time, which is why nothing caught it; the
    `--wait 90` in the README and the skill really took 100 seconds, against a
    host cap of two minutes that the same page tells the reader to stay inside.

    The fix is to re-open the stream once the socket timeout in force outlives
    what is left of the deadline. It costs one extra subscription per wait.
    """
    hub = FakeHub()
    hub.heartbeat = 20.0
    hub.stream_cost = 0.0

    assert _mail(hub, 25.0) == []
    assert hub.clock == pytest.approx(1025.0), f"it stood there for {hub.clock - 1000:.1f}s of a 25s wait"
    assert hub.calls.count("stream") == 2, "the right-sized re-open is what bounds the overrun"


def test_the_hello_frame_closes_the_subscription_race():
    """Mail that lands between the first read and the subscription is still picked up.

    The `hello` frame arrives once the subscription exists, and the waiter treats
    it like any other byte: a reason to ask the hub what is true. Nothing is lost
    in the gap.
    """
    hub = FakeHub()
    hub.chunks_per_stream = 1

    def deliver_into_the_gap(poll):
        if poll == 1:
            hub.mail = [_message(40, correlation_id=None)]

    hub.on_poll = deliver_into_the_gap

    assert _mail(hub, 60.0, limit=7) == hub.mail
    assert hub.calls == ["inbox", "stream", "inbox"]
    assert hub.limits == [7, 7], "the caller's limit did not reach every poll"
    assert hub.stream_timeouts == [pytest.approx(60.0)], "the stream outlived the deadline it was opened under"


# -- reading the module rather than running it ------------------------------------


def _wait_for_mail_body() -> str:
    """Return `wait_for_mail`'s body as source, with its docstring and every comment gone."""
    module = ast.parse(inspect.getsource(waiting))
    function = next(
        node for node in ast.walk(module) if isinstance(node, ast.FunctionDef) and node.name == "wait_for_mail"
    )
    body = function.body
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


def _imported_modules() -> set[str]:
    """Return every module name the waiting module imports, TYPE_CHECKING included."""
    module = ast.parse(inspect.getsource(waiting))
    names: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names
