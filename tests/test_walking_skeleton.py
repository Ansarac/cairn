"""The end-to-end test the whole design hangs on.

Everything else in the suite checks one module. This checks the seam: a real
hub on a real socket, a real client, two agents, a message that crosses between
them. It is the first test written and it should stay the first test read,
because the risks in this project live between modules rather than inside them.

If this file goes red, nothing else in the suite matters.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from cairn import cli, render, waiting
from cairn.client import HubClient
from cairn.errors import UsageError
from cairn.hub import make_server
from cairn.store import SqliteStore
from cairn.wire import Artifact


@pytest.fixture
def hub_server() -> Iterator[ThreadingHTTPServer]:
    """Serve a hub on an ephemeral port, backed by an in-memory database."""
    server = make_server(SqliteStore(":memory:"), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.notifier.close_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def hub(hub_server: ThreadingHTTPServer) -> HubClient:
    """Return a client pointed at the running hub."""
    host, port = hub_server.server_address[:2]
    return HubClient(f"http://{host}:{port}", timeout=5.0)


def _register(hub: HubClient, name: str, **kwargs) -> None:
    from cairn.wire import Agent

    hub.register(
        Agent(
            name=name,
            machine=kwargs.pop("machine", "testbox"),
            cwd=kwargs.pop("cwd", "/tmp/x"),
            **kwargs,
        )
    )


def test_a_name_that_moved_stops_reaching_its_old_holder(hub, tmp_path, monkeypatch):
    """Both halves of the takeover rule, over a real socket, in the order they fire.

    They are only meaningful together, which is why they are one test: the hub
    stops a newcomer inheriting the conversation, and the sender stops new mail
    following the name to whoever holds it now. Either alone leaves half the
    exchange going somewhere nobody chose.
    """
    from cairn.cli import _check_recipient
    from cairn.errors import NameMoved

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    _register(hub, "ops/dispatch", cwd="/w/ops")
    _register(hub, "rig/a", machine="bench", cwd="/w/rig")
    _check_recipient(hub, "rig/a")
    hub.send("tell", "ops/dispatch", "rig/a", "flash key is in the usual place")

    _register(hub, "rig/a", machine="some-other-box", cwd="/w/elsewhere")

    assert hub.inbox("rig/a") == []
    with pytest.raises(NameMoved) as caught:
        _check_recipient(hub, "rig/a")
    assert "bench:/w/rig" in str(caught.value)


def test_broadcast_has_no_holder_to_pin(hub, tmp_path, monkeypatch):
    """`*` is not an address anyone can take over, so the check must not fire on it."""
    from cairn.cli import _check_recipient

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    _register(hub, "ops/dispatch", cwd="/w/ops")
    _check_recipient(hub, "*")
    assert hub.send("tell", "ops/dispatch", "*", "bench down ten minutes").seq > 0


def test_a_message_crosses_between_two_agents(hub):
    """Register → tell → inbox → ack, over HTTP, with the cursor on the server."""
    _register(hub, "bench/firmware", capabilities=("hil", "jtag"))
    _register(hub, "compute/analysis", capabilities=("matlab",))

    sent = hub.send("tell", "bench/firmware", "compute/analysis", "soak run 441 failed 3 of 40 iterations")
    assert sent.seq > 0

    received = hub.inbox("compute/analysis")
    assert [m.body for m in received] == ["soak run 441 failed 3 of 40 iterations"]
    assert received[0].sender == "bench/firmware"

    hub.ack("compute/analysis", received[0].seq)
    assert hub.inbox("compute/analysis") == []


def test_the_sender_does_not_receive_its_own_message(hub):
    _register(hub, "a", machine="m1")
    _register(hub, "b", machine="m2")
    hub.send("tell", "a", "b", "hello")
    assert hub.inbox("a") == []


def test_a_peer_that_was_offline_still_gets_its_mail(hub):
    """The point of the hub: delivery does not require the peer to be listening."""
    _register(hub, "sender")
    _register(hub, "sleeper")
    for i in range(3):
        hub.send("tell", "sender", "sleeper", f"message {i}")

    # The sleeper "restarts": same name, no local state of any kind.
    _register(hub, "sleeper")
    assert [m.body for m in hub.inbox("sleeper")] == ["message 0", "message 1", "message 2"]


def test_a_brand_new_agent_starts_at_the_head_not_at_zero(hub):
    """A fresh name must not be buried under a month of other people's mail."""
    _register(hub, "a")
    _register(hub, "b")
    for i in range(5):
        hub.send("tell", "a", "b", f"old {i}")

    _register(hub, "newcomer")
    assert hub.inbox("newcomer") == []

    hub.send("tell", "a", "newcomer", "welcome")
    assert [m.body for m in hub.inbox("newcomer")] == ["welcome"]


def test_broadcast_reaches_everyone_but_the_sender(hub):
    for name in ("a", "b", "c"):
        _register(hub, name)
    hub.send("tell", "a", "*", "all hands")
    assert [m.body for m in hub.inbox("b")] == ["all hands"]
    assert [m.body for m in hub.inbox("c")] == ["all hands"]
    assert hub.inbox("a") == []


def test_a_typo_in_the_recipient_fails_loudly(hub):
    """Silently dropping a misaddressed message is the worst possible behaviour."""
    _register(hub, "a")
    _register(hub, "bench/firmware")
    with pytest.raises(UsageError, match="unknown recipient"):
        hub.send("tell", "a", "bench/firmwar", "typo")


def test_ask_and_reply_carry_the_correlation_id(hub):
    _register(hub, "asker")
    _register(hub, "answerer")
    hub.send("ask", "asker", "answerer", "do the failures correlate with temperature?", correlation_id="q-1")
    question = hub.inbox("answerer")[0]
    assert question.kind == "ask"
    assert question.correlation_id == "q-1"

    hub.send("reply", "answerer", "asker", "yes, all above 40 degrees", correlation_id="q-1")
    answer = hub.inbox("asker")[0]
    assert answer.kind == "reply"
    assert answer.correlation_id == "q-1"


def test_artifacts_survive_the_round_trip(hub):
    _register(hub, "a")
    _register(hub, "b")
    hub.send("tell", "a", "b", "capture is on the bench", artifacts=[Artifact("bench", "/srv/hil/441.bin")])
    artifact = hub.inbox("b")[0].artifacts[0]
    assert (artifact.host, artifact.path) == ("bench", "/srv/hil/441.bin")


def test_the_hub_is_healthy(hub):
    assert hub.health()["ok"] is True


# -- the bell stream -----------------------------------------------------------
#
# Same seam, one layer up: a real SSE response over a real socket, decoded by
# the same code the nudger uses. The bell is what makes delivery prompt; the
# inbox is what makes it correct. These assert the first without ever letting it
# carry the second.


def _bells(hub: HubClient, agent: str, want: int, timeout: float = 5.0) -> list[dict]:
    """Collect `want` bell payloads from a live stream, or fewer if it times out."""
    from cairn.events import sse_decode

    collected: list[dict] = []
    opened = threading.Event()

    def read() -> None:
        for event, payload in sse_decode(hub.stream(agent)):
            if event == "hello":
                opened.set()
            elif event == "mail":
                collected.append(payload)
                if len(collected) >= want:
                    return

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    assert opened.wait(timeout), "stream never opened"
    thread.join(timeout=timeout)
    return collected


def test_sending_a_message_rings_a_bell_on_the_stream(hub):
    _register(hub, "ringer")
    _register(hub, "listener")

    result: list[list[dict]] = []
    collector = threading.Thread(target=lambda: result.append(_bells(hub, "listener", want=1)), daemon=True)
    collector.start()
    time.sleep(0.4)  # let the subscription land before publishing

    hub.send("tell", "ringer", "listener", "the eval finished")
    collector.join(timeout=6.0)

    assert result, "collector thread never finished"
    bells = result[0]
    assert bells, "no bell arrived"
    assert bells[0]["seq"] > 0
    assert bells[0]["sender"] == "ringer"


def test_the_bell_never_carries_the_message_body(hub):
    """Invariant I1 at the transport layer: the stream signals, the inbox informs."""
    _register(hub, "ringer")
    _register(hub, "listener")
    body = "promote checkpoint 441 to staging immediately"

    result: list[list[dict]] = []
    collector = threading.Thread(target=lambda: result.append(_bells(hub, "listener", want=1)), daemon=True)
    collector.start()
    time.sleep(0.4)

    hub.send("tell", "ringer", "listener", body)
    collector.join(timeout=6.0)

    assert result, "collector thread never finished"
    bells = result[0]
    assert bells, "no bell arrived"
    assert body not in json.dumps(bells[0])
    assert "body" not in bells[0]


def test_the_events_route_demands_an_agent(hub):
    with pytest.raises(UsageError, match="agent"):
        next(iter(hub.stream("")))


def test_closing_the_notifier_ends_every_stream(hub, hub_server):
    """Hub shutdown must not leave handler threads blocked forever."""
    _register(hub, "listener")
    ended = threading.Event()

    def read() -> None:
        for _ in hub.stream("listener"):
            pass
        ended.set()

    threading.Thread(target=read, daemon=True).start()
    time.sleep(0.4)
    hub_server.notifier.close_all()
    assert ended.wait(timeout=5.0), "close_all left a stream open"


# -- waiting for an answer -----------------------------------------------------
#
# Both seams at once, which is why these live here rather than beside the fake
# client in test_waiting.py. `cairn inbox --wait` is a loop over the bell
# stream's raw bytes with `client.inbox` as the only thing allowed to return a
# verdict, so it is only ever as correct as the two of them together — and a
# fake proves neither. The first of these is the whole cut in one test.
#
# Three of them drive `cli.run` and assert the number it hands back. Nothing else
# in the suite does, which is why the ack ordering and the 1-versus-2 contract
# would otherwise ship green.


def test_a_waiting_reader_is_woken_by_an_answer_that_is_not_a_reply(hub):
    """The exchange this whole cut exists for, on a real socket with a real bell.

    Every filter a hand-written loop would reach for fails on this exchange, and
    one live exchange produced all of them. The answer is not a `reply`, so
    matching on kind walks past it. It carries no correlation id, so matching on
    that walks past it. Live, the peer was answering an *earlier* `tell` and its
    answer landed before the `ask` was even stored, giving it the lower sequence
    number — which is what makes "anything newer than my ask" no safer than the
    other two. That ordering is not staged here,
    because an answer already sitting in the inbox never blocks at all and so
    proves nothing about the stream. The waiter's only predicate is that the
    inbox came back non-empty.
    """
    _register(hub, "bench/firmware")
    _register(hub, "compute/analysis")
    hub.send(
        "ask", "bench/firmware", "compute/analysis", "do the failures correlate with temperature?", correlation_id="q-1"
    )

    returned: list[list] = []

    def wait() -> None:
        returned.append(waiting.wait_for_mail(hub, "bench/firmware", timeout=10.0))

    waiter = threading.Thread(target=wait, daemon=True)
    started = time.monotonic()
    waiter.start()
    time.sleep(0.4)  # let the subscription land before publishing

    hub.send("tell", "compute/analysis", "bench/firmware", "yes — every one of them is above 40 degrees")
    waiter.join(timeout=6.0)
    elapsed = time.monotonic() - started

    assert returned, "the wait never returned"
    answer = returned[0][0]
    assert answer.body == "yes — every one of them is above 40 degrees"
    assert answer.kind == "tell"
    assert answer.correlation_id is None
    assert elapsed < 5.0, f"woken by the deadline rather than by the bell, after {elapsed:.1f}s of 10"
    # And nothing was acked. Printing comes first and acking after, so a wait cut
    # short by the host's own command timeout loses no mail.
    assert [m.seq for m in hub.inbox("bench/firmware")] == [answer.seq]


def test_a_wait_that_runs_out_is_nothing_to_report_and_costs_no_mail(hub, monkeypatch, capsys):
    """A deadline that passes is exit 1, the same answer an empty inbox gives.

    "The peer said nothing" and "there was nothing there" are one fact about the
    world, so they share a code. The one that must never be shared with them is
    2, which is the next test.

    Two seconds, not one, and the elapsed assertion is not decoration. The loop
    only opens a stream above `MIN_STREAM_SECONDS` of remaining deadline, so at
    `--wait 1` this test returned in 0.000s having proved nothing but the
    argument parsing — no stream opened, no floor arithmetic, no reconnect, and
    a `waited 1s` on stderr that was a lie. It would have stayed green if
    somebody raised the floor to ten.
    """
    monkeypatch.setenv("CAIRN_AGENT", "bench/firmware")
    _register(hub, "bench/firmware")
    _register(hub, "compute/analysis")

    started = time.monotonic()
    code = cli.run(["--hub", hub.base_url, "inbox", "--wait", "2"])
    elapsed = time.monotonic() - started
    printed = capsys.readouterr()

    assert code == 1
    assert "no unread messages" in printed.out
    assert "waited 2s" in printed.err
    assert elapsed >= 2.0, f"stderr claimed two seconds and {elapsed:.2f}s passed"
    assert elapsed < 6.0, f"the wait overran its deadline by {elapsed - 2:.1f}s"
    # Standing there moved nothing: mail that arrives a moment too late is still
    # unread, so the next read gets it rather than the cursor having stepped past.
    hub.send("tell", "compute/analysis", "bench/firmware", "sorry, was on the other bench")
    assert [m.body for m in hub.inbox("bench/firmware")] == ["sorry, was on the other bench"]


def test_a_keep_alive_does_not_extend_the_deadline(hub, monkeypatch, capsys):
    """The socket timeout restarts on every read, and only a real socket shows it.

    `HTTPResponse.read1` applies the socket timeout per read, so each keep-alive
    hands the next read a fresh full budget and the wait ends at the first
    heartbeat *at or after* the deadline rather than on the deadline. Measured
    against a live hub at the shipped 20-second heartbeat: `--wait 25` returned
    in 40.01s, and the documented `--wait 90` took 100 — inside a host cap of
    two minutes that the skill tells the reader to stay under.

    The heartbeat is shortened here so the effect fits in a test: with it at 1s
    and a 2.5s deadline, the unfixed loop reads the tick at 3.0s and returns
    then. Nothing else in the suite makes the hub's own interval visible from
    the client side, and no fake can — this is the seam the `read1` bug lived in
    too.
    """
    monkeypatch.setenv("CAIRN_AGENT", "bench/firmware")
    monkeypatch.setattr("cairn.hub.HEARTBEAT_SECONDS", 1.0)
    _register(hub, "bench/firmware")

    started = time.monotonic()
    assert cli.run(["--hub", hub.base_url, "inbox", "--wait", "2.5"]) == 1
    elapsed = time.monotonic() - started
    capsys.readouterr()

    assert elapsed >= 2.5, f"it returned after {elapsed:.2f}s of a 2.5s deadline"
    assert elapsed < 2.9, f"it waited for the hub's next keep-alive, not its own deadline: {elapsed:.2f}s"


def test_a_deadline_that_cannot_be_stood_through_is_refused_before_anything_else(capsys):
    """No hub fixture, deliberately: this must be decided before the network is touched.

    `infinity` is the one to keep. `float` accepts it, it survives a `> 0` test,
    and `socket.settimeout(inf)` raises `OverflowError` — not an `OSError`, so
    `client.stream` does not convert it and `run()` does not catch it. Live,
    `cairn inbox --wait infinity` printed a traceback and exited **1**: a
    poisoned read wearing the code for "nothing to report", which is the exact
    shape this cut removed from `store.append`. `nan` is the mirror — every
    comparison against it is false, so it passed the old guard and then never
    waited, reporting `waited nans, still nothing`.
    """
    for spelling in ("0", "-5", "inf", "infinity", "1e400", "nan"):
        assert cli.run(["inbox", "--wait", spelling]) == 3, f"--wait {spelling} was not refused"
        assert "--wait needs" in capsys.readouterr().err


def test_a_hub_that_dies_mid_wait_is_two_not_one(hub, hub_server, monkeypatch, capsys):
    """An outage during a wait must not read as a peer's silence.

    Two seconds rather than a realistic sixty because what is under test is
    which number comes back, not how long the command was willing to stand
    there; the deadline is spent in real wall time either way.

    This is the assertion that stops 1 and 2 collapsing in the direction that
    hides an outage. `client.stream` returns *silently* when its socket dies, so
    a loop that let the stream end the wait would report "your peer said
    nothing" while the truth was "nobody heard you" — and no renderer runs, so
    stdout is empty rather than a rendering of an empty inbox.

    The stderr text is coupled to the two seconds, and deliberately: with the
    deadline this short there is nothing left to reconnect with, so the outage
    surfaces from the *final `inbox`* — the one call the exit-code contract says
    a verdict may come from. Raise the deadline and it surfaces from the stream
    reconnect instead, as `cannot open the bell stream at …`. Measured live: the
    hub killed 1s into a `--wait 20` exited 2 with empty stdout and that second
    wording. Exit 2 either way, which is the contract; if this assertion goes
    red after someone lengthens the wait, that is what happened.
    """
    monkeypatch.setenv("CAIRN_AGENT", "bench/firmware")
    _register(hub, "bench/firmware")

    codes: list[int] = []

    def read() -> None:
        codes.append(cli.run(["--hub", hub.base_url, "inbox", "--wait", "2"]))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    time.sleep(0.4)
    # The fixture tears the hub down the same way, and every step is idempotent.
    hub_server.notifier.close_all()
    hub_server.shutdown()
    hub_server.server_close()
    reader.join(timeout=6.0)
    printed = capsys.readouterr()

    assert codes, "the wait never returned"
    assert codes[0] == 2
    assert printed.out == ""
    assert "cannot reach hub" in printed.err


def test_a_hub_with_no_event_route_still_waits_and_still_says_nothing_came(hub, hub_server, monkeypatch, capsys):
    """A hub that answers the inbox and not the bell stream is a slow wait, not an outage.

    This is the hub that predates cut 2, and it is the cross-version case the
    whole cut is shaped around: no new route means a build from this cut talks
    to a hub older than itself. `client.stream` raises `Unreachable` on the 404
    though, so before `_ticks` this returned exit **2** in 0.000s with the whole
    deadline unspent — "nobody heard you", microseconds after `/v1/inbox` had
    answered. The same shape covers any ingress that will not pass
    `text/event-stream`, and a reconnect that lands in the second a restarted
    hub is not yet listening.

    The route is removed from the dispatch table rather than made to fail, so
    the 404 is the hub's own, byte for byte.
    """

    def read_routes_without_the_bell_stream(self) -> None:
        self._dispatch({"/v1/health": self._health, "/v1/peers": self._peers, "/v1/inbox": self._inbox})

    monkeypatch.setenv("CAIRN_AGENT", "bench/firmware")
    _register(hub, "bench/firmware")
    monkeypatch.setattr(hub_server.RequestHandlerClass, "do_GET", read_routes_without_the_bell_stream)

    started = time.monotonic()
    code = cli.run(["--hub", hub.base_url, "inbox", "--wait", "2"])
    elapsed = time.monotonic() - started
    printed = capsys.readouterr()

    assert code == 1, "a missing event route was reported as an unreachable hub"
    assert "no unread messages" in printed.out
    assert elapsed >= 2.0, f"it gave up after {elapsed:.2f}s of a 2s deadline"


def test_a_waited_read_is_framed_exactly_like_an_ordinary_one(hub, monkeypatch, capsys):
    """Mail that was waited for arrives inside the same frame as mail that was not.

    There is one renderer and one ack path — that is what `--wait` being a flag
    rather than a verb buys — so what is worth asserting is the two things a
    second code path would have broken. The framing is inherited whole. And
    stderr stays empty on a successful read: the "still nothing" line is bounded
    to the empty case, and a draft of this cut printed it after a `--no-ack`
    read that had just printed mail, telling the agent nothing had arrived.
    """
    monkeypatch.setenv("CAIRN_AGENT", "bench/firmware")
    _register(hub, "bench/firmware")
    _register(hub, "compute/analysis")
    hub.send("tell", "compute/analysis", "bench/firmware", "plot is on the share")

    assert cli.run(["--hub", hub.base_url, "inbox", "--wait", "5", "--no-ack"]) == 0
    peeked = capsys.readouterr()
    assert render.CLAIM_CLAUSE in peeked.out
    assert "UNVERIFIED" in peeked.out
    assert peeked.err == ""
    assert [m.body for m in hub.inbox("bench/firmware")] == ["plot is on the share"]

    assert cli.run(["--hub", hub.base_url, "inbox", "--wait", "5"]) == 0
    read = capsys.readouterr()
    assert render.CLAIM_CLAUSE in read.out
    assert "UNVERIFIED" in read.out
    assert read.err == ""
    assert hub.inbox("bench/firmware") == []


def test_the_hub_refuses_a_kind_it_would_poison_its_own_readers_with(hub):
    """A kind the wire rejects must not be storable, or that mailbox never reads again.

    Reproduced against a live hub before it was fixed. `hub._send` handed
    `obj.get("kind", "tell")` straight to `store.append`, which checked the
    sender and the recipient but not the kind, while `Message.from_json` checks
    it on the way back out. So one POST was stored durably and answered 200, and
    from then on every `cairn inbox` for that recipient raised `WireError` — a
    `ValueError`, which `run()` deliberately does not catch — giving a traceback
    and exit 1, which is the code for "nothing to report". The mailbox read as
    empty to every script forever, with no seq printed to aim an `ack` past.
    `cairn inbox --wait` would turn that from a broken read into a hang.
    """
    _register(hub, "compute/analysis")
    _register(hub, "bench/firmware")
    request = urllib.request.Request(  # noqa: S310 - the URL is this test's own loopback hub
        f"{hub.base_url}/v1/messages",
        data=json.dumps(
            {
                "v": 1,
                "kind": "shout",
                "sender": "compute/analysis",
                "recipient": "bench/firmware",
                "body": "everybody stop what you are doing",
                "artifacts": [],
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5.0).close()  # noqa: S310 - same URL, built above

    assert caught.value.code == 400
    assert "shout" in caught.value.read().decode()
    assert hub.inbox("bench/firmware") == []

    hub.send("tell", "compute/analysis", "bench/firmware", "ignore that, wrong channel")
    assert [m.body for m in hub.inbox("bench/firmware")] == ["ignore that, wrong channel"]


# -- what two sessions using this for real walked into --------------------------
#
# Both of these were found by agent sessions given the skill, a job, and nothing
# else. Neither was reachable from a unit test, and neither is about the code
# being wrong so much as the surface not being the shape its own documentation
# promised.


def test_an_answer_can_carry_a_reference_to_what_it_produced(hub):
    """`reply` accepts `-a`, because an answer is the send most likely to need it.

    A peer session on shift read the skill's rule — anything bigger than prose
    goes behind a path — as the universal rule it is written as, ran
    `cairn reply … -a HOST:PATH`, and got `unrecognized arguments`. `tell` and
    `ask` took it; `reply` did not. It folded the path into its prose instead,
    which works and is exactly the habit the rule exists to prevent.
    """
    _register(hub, "bench/firmware")
    _register(hub, "compute/analysis", machine="compute")
    hub.send("ask", "bench/firmware", "compute/analysis", "can you run the correlation?", correlation_id="q-1")

    answer = hub.send(
        "reply",
        "compute/analysis",
        "bench/firmware",
        "done — plot attached, the knee is at 39 degrees",
        correlation_id="q-1",
        artifacts=[Artifact("compute", "/srv/analysis/441/knee.png")],
    )
    assert answer.kind == "reply"

    received = hub.inbox("bench/firmware")[0]
    assert received.correlation_id == "q-1"
    assert (received.artifacts[0].host, received.artifacts[0].path) == ("compute", "/srv/analysis/441/knee.png")


def test_a_malformed_command_line_is_not_an_unreachable_hub(hub, monkeypatch, capsys):
    """Exit 3 for "you asked for something impossible", never 2 for "nobody heard you".

    argparse exits **2** on a bad command line, and 2 is cairn's code for an
    unreachable hub — so until this was fixed, a typo reported itself as a
    network outage. Measured on a session that mistyped a flag and spent a moment
    wondering whether the hub had gone; a script doing
    `cairn reply … || echo "hub down"` would have said it out loud and been wrong.

    The last two assertions are the ones that make this a fix rather than a
    trade: `--help` still leaves 0, and a genuinely unreachable hub is still 2.
    """
    monkeypatch.setenv("CAIRN_AGENT", "bench/firmware")
    _register(hub, "bench/firmware")

    for argv in (
        ["reply", "compute/analysis", "q-1", "body", "-Z", "nonsense"],
        ["inbox", "--wait", "sixty"],
        ["inbox", "--limit", "lots"],
        [],
    ):
        assert cli.run(["--hub", hub.base_url, *argv]) == 3, f"{argv} did not exit 3"
        assert capsys.readouterr().err.startswith("cairn: ")

    with pytest.raises(SystemExit) as helped:
        cli.run(["--help"])
    assert helped.value.code == 0

    capsys.readouterr()
    assert cli.run(["--hub", "http://127.0.0.1:1", "peers"]) == 2
    assert "cannot reach hub" in capsys.readouterr().err


# -- what a session leaves behind ----------------------------------------------
#
# Cut 4, and the same seam with one thing taken out of it: a note has no
# recipient. So nothing below is addressed to a session, nothing below is
# consumed by reading, and nothing below may ring. Two of these drive the whole
# command surface — `register`, `tell`, `note`, `settle`, `notes` — from separate
# working directories against one hub, because the entire claim being made is
# that a note is still there for somebody who was not in the conversation, and a
# claim about somebody who was not there cannot be tested inside one module.


def _cli(hub: HubClient, *argv: str) -> int:
    """Run one cairn command against the live hub, the way that session's shell would."""
    return cli.run(["--hub", hub.base_url, *argv])


def test_a_question_outlives_the_session_that_asked_it(hub, tmp_path, monkeypatch, capsys):
    """The exchange this cut exists for: the session goes, its open loop does not.

    Cut 3's live run produced it unprompted and docs/design.md §12 item 4 records
    it. One of the two sessions was on a machine being handed to another team,
    and when it ended it took its open questions with it — a message is addressed
    to a session, and the session was the thing that went away. The surviving
    peer reconstructed them into its own shift log under a heading it invented.

    So what is asserted here is a contrast, and both halves are measured against
    one hub in one run. The going-away is the real one: the same name registered
    from another machine and another directory, which `store.register` treats as
    a takeover and which parks the mailbox cursor at the head. The mail addressed
    to that name goes with it — stated on the way past, and out of reach. The
    note and the question the same session wrote are still on their subject, and
    a peer that was told nothing by any message finds them, answers one, and
    files the answer under a subject it never names.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Identity here is per-directory, which is the whole mechanism by which these
    # are two sessions rather than one; an inherited $CAIRN_AGENT would override it.
    monkeypatch.delenv("CAIRN_AGENT", raising=False)
    bench, compute, newcomer = (tmp_path / name for name in ("bench", "compute", "newcomer"))
    for directory in (bench, compute, newcomer):
        directory.mkdir()

    monkeypatch.chdir(bench)
    assert _cli(hub, "register", "bench/firmware", "--machine", "bench") == 0
    monkeypatch.chdir(compute)
    assert _cli(hub, "register", "compute/analysis", "--machine", "compute") == 0
    assert _cli(hub, "tell", "bench/firmware", "the correlation plot is on the share") == 0
    capsys.readouterr()

    # The bench session writes down what it knows and what it does not, and never
    # reads the mail it was sent.
    monkeypatch.chdir(bench)
    assert _cli(hub, "note", "rig-a", "the flash jig needs the 3v3 rail jumpered before a soak run") == 0
    assert _cli(hub, "note", "rig-a", "does the iteration-12 failure survive the older bootloader?", "-q") == 0
    capsys.readouterr()

    # And then it is gone: the rig went to another team, and the name comes back
    # from a machine and a directory that are not the ones that held it.
    monkeypatch.chdir(newcomer)
    assert _cli(hub, "register", "bench/firmware", "--machine", "handover-box") == 0
    handover = capsys.readouterr().out
    assert "no longer in your inbox" in handover, "the takeover did not report the mail it stepped over"
    assert "unanswered question" in handover, "the same arrival that reported the loss said nothing about the question"
    assert hub.inbox("bench/firmware") == [], "the takeover was a no-op; nothing was actually left behind"

    kept, total = hub.notes("rig-a")
    assert total == 2, "the session ended and took its sediment with it"
    assert [entry.is_open for entry in kept] == [False, True]

    # The peer was never told any of this. Nothing was ever addressed to it.
    monkeypatch.chdir(compute)
    assert hub.inbox("compute/analysis") == []
    assert _cli(hub, "register", "compute/analysis", "--machine", "compute") == 0
    arrival = capsys.readouterr().out
    assert "1 unanswered question" in arrival
    assert "cairn notes --open" in arrival

    assert _cli(hub, "notes", "--open", "--json") == 0
    found = json.loads(capsys.readouterr().out)
    assert found["open_questions"] == 1
    question = found["notes"][0]
    assert question["open"] is True
    assert "older bootloader" in question["body"]
    assert question["author"] == "bench/firmware"

    # Settling names an id and an artifact, and no subject anywhere.
    answer = "40 of 40 clean on the older bootloader, so it is the new one"
    assert _cli(hub, "settle", str(question["id"]), answer, "-a", "compute:/srv/analysis/441/soak.csv") == 0
    filed = capsys.readouterr().out
    assert f"settles question {question['id']}" in filed
    assert "rig-a" in filed, "the answer was not filed under the subject of the question it settles"

    assert _cli(hub, "notes", "--open") == 1, "the settled question is still being offered as unanswered"
    assert "no open questions" in capsys.readouterr().out

    pile, total = hub.notes("rig-a")
    assert total == 3, "settling replaced the record instead of adding to it"
    assert [entry.is_open for entry in pile] == [False, False, False]
    assert pile[1].settled_by == pile[2].note.id
    assert pile[2].note.subject == "rig-a"
    assert pile[2].note.artifacts[0].path == "/srv/analysis/441/soak.csv"


def test_a_note_rings_no_bell_though_a_message_still_does(hub):
    """Sediment must not become mail — invariant I2, asserted on the wire.

    A note has no recipient, so the only way to ring for one is to ring
    everybody, and a hub that interrupted every session on it each time somebody
    filed a fact would have handed the writer control over when peers read.
    `hub._write_note` keeps that invariant by *not* calling `notifier.publish`.
    An absence is the kind of thing a well-meaning patch puts back, and the kind
    of thing nothing short of a live stream can be asked about.

    The ordering is what makes the silence provable rather than lucky. Both
    streams are open before the note is written and the `tell` goes out after it,
    and bells arrive on a subscription in order — so if the note had rung, the
    first frame the peer decoded would be the note's rather than the message's.
    A test that merely waited a while and saw nothing would prove the network
    was quiet.
    """
    _register(hub, "bench/firmware", machine="bench")
    _register(hub, "compute/analysis", machine="compute")

    author: list[list[dict]] = []
    peer: list[list[dict]] = []
    listeners = [
        threading.Thread(target=lambda: author.append(_bells(hub, "bench/firmware", want=1, timeout=3.0)), daemon=True),
        threading.Thread(target=lambda: peer.append(_bells(hub, "compute/analysis", want=1, timeout=3.0)), daemon=True),
    ]
    for listener in listeners:
        listener.start()
    time.sleep(0.4)  # let both subscriptions land before anything is written

    hub.write_note("bench/firmware", "the flash jig needs the 3v3 rail jumpered", subject="rig-a", question=True)
    time.sleep(0.4)  # a bell for that note would be on both streams by now
    sent = hub.send("tell", "bench/firmware", "compute/analysis", "soak run 441 failed 3 of 40 iterations")
    for listener in listeners:
        listener.join(timeout=8.0)

    assert peer, "the peer's collector never finished"
    assert peer[0], "no bell arrived for the message"
    assert peer[0][0].get("seq") == sent.seq, "the first bell on the peer's stream was the note, not the message"
    assert author, "the author's collector never finished"
    assert author[0] == [], "writing a note rang a bell"


def test_reading_the_pile_consumes_none_of_it_and_moves_no_cursor(hub, tmp_path, monkeypatch, capsys):
    """A pile is not a queue: the next reader has to find exactly what this one did.

    Two failures at once, because notes and messages share a hub, a client and a
    store. If reading a subject filed a cursor the way `inbox` does, the peer who
    turns up tomorrow finds it empty and sediment is just a message with extra
    steps. And if a note read touched the *message* cursor — the one thing on
    this hub a read is allowed to move — it would silently ack mail nobody has
    seen, which is the failure `cmd_inbox` flushes before acking to avoid.

    `GET /v1/notes` carries no `?agent=` at all, which is what makes both
    impossible rather than merely unlikely. Asserted end to end anyway: the seam
    is where a guarantee that holds inside every module quietly stops holding.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bench, compute = (tmp_path / name for name in ("bench", "compute"))
    for directory in (bench, compute):
        directory.mkdir()
    _register(hub, "bench/firmware", machine="bench", cwd=str(bench))
    _register(hub, "compute/analysis", machine="compute", cwd=str(compute))
    hub.send("tell", "compute/analysis", "bench/firmware", "the knee is at 39 degrees")
    hub.send("tell", "bench/firmware", "compute/analysis", "soak run 441 failed 3 of 40 iterations")
    waiting_mail = {name: [m.seq for m in hub.inbox(name)] for name in ("bench/firmware", "compute/analysis")}
    assert [len(seqs) for seqs in waiting_mail.values()] == [1, 1], "nothing was pending, so nothing could be lost"
    hub.write_note("bench/firmware", "the flash jig needs the 3v3 rail jumpered", subject="rig-a")
    hub.write_note("bench/firmware", "does iteration 12 survive the older bootloader?", subject="rig-a", question=True)

    monkeypatch.chdir(compute)
    assert _cli(hub, "notes", "rig-a") == 0
    read_first = capsys.readouterr().out
    monkeypatch.chdir(bench)
    assert _cli(hub, "notes", "rig-a") == 0
    read_second = capsys.readouterr().out

    assert "OPEN" in read_first
    assert read_second == read_first, "the second reader did not find what the first one read"
    assert {name: [m.seq for m in hub.inbox(name)] for name in waiting_mail} == waiting_mail


def test_a_hub_that_predates_notes_still_registers_and_says_nothing_about_them(
    hub, hub_server, tmp_path, monkeypatch, capsys
):
    """A missing `/v1/subjects` is "no answer", never "nobody heard you".

    `client._call` maps a 404 to `Unreachable`, so an unguarded
    `client.subjects()` on the registration path exits **2** — cairn's code for
    an outage — against a hub that is up, healthy and carrying messages
    perfectly, purely because it was built before this cut. That is cut 3's
    `/v1/events` failure again, arriving at the one command a new build runs
    first against whatever hub is already deployed. `cli._open_questions`'s catch
    is the guard, and a dead port is not a test of it: that fails at connect,
    while the case this cut is shaped around is a hub that answers everything
    else and 404s one route.

    The route is removed from the dispatch table rather than made to fail, so the
    404 is the hub's own, byte for byte. The healthy hub is registered against
    first in the same run, so the hint being absent afterwards means the guard
    fired rather than that there was nothing to say.
    """
    from cairn.errors import Unreachable

    def read_routes_from_before_notes(self) -> None:
        self._dispatch(
            {
                "/v1/health": self._health,
                "/v1/peers": self._peers,
                "/v1/inbox": self._inbox,
                "/v1/events": self._events,
            }
        )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CAIRN_AGENT", raising=False)
    monkeypatch.chdir(tmp_path)
    _register(hub, "bench/firmware", machine="bench")
    hub.write_note("bench/firmware", "does iteration 12 survive the older bootloader?", subject="rig-a", question=True)

    assert _cli(hub, "register", "compute/analysis", "--machine", "compute") == 0
    assert "unanswered question" in capsys.readouterr().out

    monkeypatch.setattr(hub_server.RequestHandlerClass, "do_GET", read_routes_from_before_notes)
    with pytest.raises(Unreachable):
        hub.subjects()

    assert _cli(hub, "register", "compute/analysis", "--machine", "compute") == 0, "an old hub read as an outage"
    printed = capsys.readouterr()
    assert "registered as compute/analysis" in printed.out
    assert "unanswered" not in printed.out
    assert printed.err == ""


def test_the_json_pile_says_how_much_it_is_not_showing(hub, capsys):
    """A truncated pile that reads as a whole one is the defect `cairn inbox` already has.

    `inbox` cuts at `--limit` and says nothing, which is how the turn-boundary
    bell goes permanently deaf past that limit — docs/design.md's appendix
    carries the row. Notes are the surface that fixed it, and the fix only counts
    over the wire: the hub serializes `total` by hand beside the page,
    `client.notes` hands both back as a pair, and a program that never sees the
    second number reports somebody's newest two notes as everything anyone has
    ever written about the rig.

    The bodies are asserted as well as the counts, because the page is the
    *newest* matches printed oldest-first: truncation is meant to drop ancient
    sediment while the reading order stays chronological, and getting either half
    of that backwards is invisible to a count.
    """
    _register(hub, "bench/firmware", machine="bench")
    for iteration in range(5):
        hub.write_note("bench/firmware", f"soak iteration {iteration} logged", subject="rig-a")

    assert _cli(hub, "notes", "rig-a", "--limit", "2", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert (payload["showing"], payload["total"]) == (2, 5)
    assert [n["body"] for n in payload["notes"]] == ["soak iteration 3 logged", "soak iteration 4 logged"]
    assert payload["framing"]["notice"] == render.NOTES_NOTICE


def test_an_empty_pile_still_arrives_framed(hub, capsys):
    """`--json` must not become the one path where peer content arrives unframed.

    It was, once: `inbox_json` carried no framing block at all, so a program
    reading JSON got peer text with nothing attached saying it was a claim. The
    fix is to emit the block whether or not there is anything to frame, and the
    empty case is where that gets lost — "there is nothing to frame" sounds
    reasonable exactly there, and a caller forced to special-case the empty shape
    is a caller that will special-case the full one next.

    Exit 1 rides along because an empty subject is an answer. Collapsing it into
    2 would say the hub was unreachable while it was answering.
    """
    assert _cli(hub, "notes", "rig-b", "--json") == 1
    payload = json.loads(capsys.readouterr().out)

    assert (payload["notes"], payload["total"]) == ([], 0)
    assert payload["framing"] == {"source": "peer-agents", "authority": "none", "notice": render.NOTES_NOTICE}


def test_a_restarted_session_recovers_what_it_said_from_the_hub(hub, tmp_path, monkeypatch, capsys):
    """Cut 5's whole reason for existing, over a real socket.

    The friction is recorded in docs/design.md §12 item 3 and was produced by a
    live run: two sessions held a twenty-minute exchange with three correlation
    ids in flight and tracked them in scrollback, because nothing else would.
    Scrollback is the thing a restart destroys. `cairn inbox` shows only what
    *arrived*, so after a restart a session knows what it was told and has no
    record at all of what it told anyone.

    The restart here is the real one the store already models — the same name
    re-registering from the same `(machine, cwd)`, which `register` treats as a
    returning session. What the log has to survive is that registration, and the
    correlation ids are asserted individually because they are the specific thing
    the session could not hold in its head.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bench = tmp_path / "bench"
    bench.mkdir()
    monkeypatch.chdir(bench)
    _register(hub, "compute/traces", machine="compute", cwd="/w/compute")
    assert _cli(hub, "register", "bench/night-shift", "--machine", "bench") == 0
    capsys.readouterr()

    assert _cli(hub, "tell", "compute/traces", "cold-start failure at iteration 33 reproduced, 1 in 3") == 0
    assert _cli(hub, "ask", "compute/traces", "can you read a CTF trace?", "--correlation", "q-7591dac1") == 0
    newer = "can the tracer write to the shared path?"
    assert _cli(hub, "ask", "compute/traces", newer, "--correlation", "q-d9698ba3") == 0
    capsys.readouterr()

    # The peer answers the *older* question while the newer one is outstanding —
    # the exact ordering the live run produced, and the reason the session could
    # not tell from its inbox alone which of its own questions was still open.
    hub.send("reply", "compute/traces", "bench/night-shift", "yes, send it", correlation_id="q-7591dac1")

    # The restart: same name, same machine, same directory.
    assert _cli(hub, "register", "bench/night-shift", "--machine", "bench") == 0
    capsys.readouterr()

    assert _cli(hub, "sent") == 0
    printed = capsys.readouterr().out

    assert "q-7591dac1" in printed
    assert "q-d9698ba3" in printed, "the outstanding question was not in the session's own record"
    assert "cold-start failure at iteration 33" in printed
    assert "yes, send it" not in printed, "the peer's reply leaked into the sender's own log"
    assert render.SENT_CLAUSE in printed
    assert "not what anyone answered" in printed


def test_reading_the_sent_log_consumes_nothing_and_rings_nobody(hub, tmp_path, monkeypatch, capsys):
    """Both absences, asserted together because both are things a patch adds back.

    **No cursor.** You have seen your own sends by definition, so there is
    nothing here to consume — and the cursor a careless read could touch is the
    one holding *unread mail*, where moving it acks messages nobody has seen.
    That is the failure `cmd_inbox` flushes before acking to avoid, reachable
    from a command that never intended to ack anything.

    **No bell.** A read is not an event. Ringing here would be worse than the
    note case invariant I2 already forbids, because the only plausible recipient
    of such a bell is the peer whose message is being looked back at — turning a
    private read into a notification.

    The peer's stream is open before the read and a real `tell` is sent after it,
    so the silence is provable rather than lucky: if the read had rung, the first
    frame decoded would be its, not the message's.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bench = tmp_path / "bench"
    bench.mkdir()
    monkeypatch.chdir(bench)
    _register(hub, "compute/traces", machine="compute", cwd="/w/compute")
    assert _cli(hub, "register", "bench/night-shift", "--machine", "bench") == 0
    hub.send("tell", "compute/traces", "bench/night-shift", "the knee is at 39 degrees")
    hub.send("tell", "bench/night-shift", "compute/traces", "derate is in place")
    capsys.readouterr()

    unread_before = [m.seq for m in hub.inbox("bench/night-shift")]
    assert unread_before, "no unread mail was waiting, so the cursor assertion proves nothing"

    peer: list[list[dict]] = []
    listener = threading.Thread(
        target=lambda: peer.append(_bells(hub, "compute/traces", want=1, timeout=3.0)), daemon=True
    )
    listener.start()
    time.sleep(0.4)  # let the subscription land before anything is read

    assert _cli(hub, "sent") == 0
    first = capsys.readouterr().out
    assert _cli(hub, "sent") == 0
    assert capsys.readouterr().out == first, "a second read did not find what the first one did"

    time.sleep(0.4)  # a bell for either read would be on the stream by now
    rung = hub.send("tell", "bench/night-shift", "compute/traces", "capture staged")
    listener.join(timeout=8.0)

    assert peer, "the peer's collector never finished"
    assert peer[0], "no bell arrived for the message, so the silence proves nothing"
    assert peer[0][0].get("seq") == rung.seq, "the first bell on the peer's stream was a sent read"
    assert [m.seq for m in hub.inbox("bench/night-shift")] == unread_before, "reading the log moved the mail cursor"


def test_a_hub_that_predates_the_sent_log_says_so_as_a_refusal_not_an_outage(hub, hub_server, tmp_path, monkeypatch):
    """The cross-version case every additive route has to answer for.

    `/v1/sent` does not exist on a hub built before cut 5, and `client._call`
    maps a 404 to `Unreachable` — exit 2, "the hub could not be reached", on a
    hub that is up and carrying messages fine. `cairn register` guards against
    exactly this for `/v1/subjects` because a courtesy line must not fail a
    command that succeeded.

    `cairn sent` is **not** guarded, and that is the right answer rather than an
    oversight: the route is the whole command, so there is no successful command
    left to protect. What matters is that the failure is loud and immediate
    instead of an empty log — "this hub has no record for you" and "this hub
    cannot answer that" are opposites, and printing the first would tell a
    restarted session it had said nothing all shift.
    """
    from cairn.errors import Unreachable

    def read_routes_from_before_the_sent_log(self) -> None:
        self._dispatch(
            {
                "/v1/health": self._health,
                "/v1/peers": self._peers,
                "/v1/inbox": self._inbox,
                "/v1/events": self._events,
                "/v1/notes": self._notes,
                "/v1/subjects": self._subjects,
            }
        )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bench = tmp_path / "bench"
    bench.mkdir()
    monkeypatch.chdir(bench)
    _register(hub, "compute/traces", machine="compute", cwd="/w/compute")
    assert _cli(hub, "register", "bench/night-shift", "--machine", "bench") == 0
    assert _cli(hub, "tell", "compute/traces", "capture staged") == 0
    assert _cli(hub, "sent") == 0, "the healthy hub did not answer, so the old one proves nothing"

    monkeypatch.setattr(hub_server.RequestHandlerClass, "do_GET", read_routes_from_before_the_sent_log)
    with pytest.raises(Unreachable):
        hub.sent("bench/night-shift")
    assert _cli(hub, "sent") == 2, "an old hub read as an empty log rather than as a refusal"


def test_the_sent_json_says_how_much_it_is_not_showing(hub, tmp_path, monkeypatch, capsys):
    """`total` has to survive the wire, the same way it does for notes.

    A page indistinguishable from a complete history is the defect `cairn inbox`
    still has, and on this surface it is worse: the reader is a restarted session
    asking "what have I already said", and a silently truncated answer is one it
    will act on by repeating itself.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bench = tmp_path / "bench"
    bench.mkdir()
    monkeypatch.chdir(bench)
    _register(hub, "compute/traces", machine="compute", cwd="/w/compute")
    assert _cli(hub, "register", "bench/night-shift", "--machine", "bench") == 0
    for iteration in range(5):
        assert _cli(hub, "tell", "compute/traces", f"soak iteration {iteration} logged") == 0
    capsys.readouterr()

    assert _cli(hub, "sent", "--limit", "2", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert (payload["showing"], payload["total"]) == (2, 5)
    assert [m["body"] for m in payload["messages"]] == ["soak iteration 3 logged", "soak iteration 4 logged"]
    assert payload["framing"] == {"source": "hub-record-of-self", "authority": "none", "notice": render.SENT_NOTICE}


def test_an_empty_sent_log_is_an_answer_and_names_the_hub(hub, tmp_path, monkeypatch, capsys):
    """Exit 1, not 2, and the hub named — the rule every "nothing" branch re-applies."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bench = tmp_path / "bench"
    bench.mkdir()
    monkeypatch.chdir(bench)
    assert _cli(hub, "register", "bench/night-shift", "--machine", "bench") == 0
    capsys.readouterr()

    assert _cli(hub, "sent") == 1
    printed = capsys.readouterr().out
    assert "nothing sent from here yet" in printed
    assert hub.base_url in printed, "an empty answer that does not name the hub is two answers at once"
