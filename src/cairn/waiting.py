"""When to stop waiting for mail, and what is allowed to decide it.

This is the whole of `cairn inbox --wait`. It is a rule, not transport, which is
why it is not in `client.py`, and not a subcommand's business, which is why it is
not in `cli.py`.

Four properties are structural here rather than checked, and each replaces a
loop somebody would otherwise write by hand and get wrong.

**It cannot block on a question that was already answered.** The first thing it
does is read. A peer that answered before the question landed — which happened
live, and is why docs/design.md §12 item 3 rules out all three of kind,
correlation id and "anything newer than my ask" — has already put mail in the
inbox, so the first read returns it and nothing blocks.

**It cannot skip an answer that arrived out of band.** It never inspects a
message, never decodes a bell frame, and has no filter: the bytes off the stream
are a reason to ask the hub what is true, nothing more. Anything unread ends the
wait and the caller reads all of it. The kinds are a hint that an answer is
expected, not something to wait on — and in the live exchange that taught this
the peer was answering an *earlier* `tell`, seconds before the `ask` landed, so
the answer settled a question it never saw and carried the *lower* sequence
number. "Watch for anything after my ask" fails on it for the same reason a kind
filter does.

**Only the hub may say "nothing came".** Every verdict, including the empty one,
is the return value of an `inbox` call — never the expiry of a clock, never the
end of a stream, and never the stream's refusal to open. `client.stream` returns
silently when its socket dies, so a wait that concluded from the stream would
report "your peer said nothing" (exit 1) when the truth is "nobody heard you"
(exit 2); and it *raises* when the event route is not there, so a wait that let
that through would report an outage (exit 2) on a hub that had just answered a
read. `_ticks` is where the second half of that is enforced.

**It never acknowledges.** Advancing the read position is the caller's act,
taken after it has printed — so a wait cut short by the host's own command
timeout, which is two minutes on the product this was measured against, loses
nothing, because nothing had been marked read.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

from cairn.errors import CairnError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cairn.client import HubClient
    from cairn.wire import InboxPage

MIN_STREAM_SECONDS = 1.0
"""At or below this much deadline left, do not open a stream — just poll and stop.

Two reasons, and only the second is measured. The stream is opened with whatever
is left of the deadline as its socket timeout, and `client.stream` raises when a
connection cannot be established inside it; on a link where the connect and the
response headers can eat the last scraps of a wait, that is a stream attempt
whose only possible outcome is a failure. Read out of `client.stream`'s error
path rather than observed — 300 loopback connects here had a median of 0.038 ms
and a maximum of 0.142 ms, so nothing on this machine comes close. `_ticks`
demotes that failure to silence anyway, which leaves the plain one: a
subscription the hub has to register, fan out to and reap costs more than a
second of waiting is worth, and the trailing poll answers either way.
"""

TICK_FLOOR = 5.0
"""The least wall time one stream attempt may account for before another opens.

In the ordinary case this never applies: the hub writes a keep-alive on its own
interval, so one stream lives for the whole wait. Measured with the hub's
heartbeat shortened to 0.4s, `client.stream` yielded bytes at 0.4, 0.8 and 1.2
seconds — one tick per heartbeat, with no timer of our own. This bounds the
other case, where the stream yields nothing and does so immediately and forever:
a proxy that strips SSE, or a hub answering the inbox but not the event route —
that second one raises rather than ending, and `_ticks` turns it into the same
silence. Without the floor that loop reconnects and polls as fast as the network
allows, and every poll is a durable write on the hub's single connection; the
test that pins this counts the polls with and without it — 12 reconnects against
1181 over the same 60 seconds. With the floor, that failure degrades to a
five-second poll: slower, still correct.
"""


def _ticks(client: HubClient, agent: str, timeout: float) -> Iterator[bytes]:
    """Yield one reason to ask the hub per chunk off the bell stream — at least one.

    A stream is a reason to go and ask, never a verdict, and that has to hold for
    the stream's own failures as well as for what it carries. Without the
    suppression, a hub that answers `/v1/inbox` and 404s `/v1/events` turns
    `cairn inbox --wait 60` into "hub unreachable" (exit 2) microseconds after
    the first read proved the hub was up, with sixty seconds of deadline unspent.
    Three ways to arrive there, none exotic: a hub built before the bell stream
    existed — precisely the cross-version case this cut is shaped to keep
    working — any ingress that will not pass `text/event-stream`, and a reconnect
    landing in the second a restarted hub is not yet listening.

    The trailing tick is the other half. A stream that produced nothing at all
    gives the loop above nothing to poll on, so without it the same hub degrades
    from a wrong answer to a right answer sixty seconds late, and `TICK_FLOOR`'s
    promise of a five-second poll is only true for streams healthy enough to send
    a `hello`. One tick per attempt, floored, is that promise.

    What is lost against a working stream is promptness. What must not be lost is
    the distinction: the `inbox` calls in the loop are outside the suppression, so
    a hub that has genuinely gone away is still reported as exit 2 by the next of
    them — within a floor period, not at the deadline.
    """
    ticked = False
    with contextlib.suppress(CairnError), contextlib.closing(client.stream(agent, timeout=timeout)) as bells:
        for chunk in bells:
            ticked = True
            yield chunk
    if not ticked:
        yield b""


def wait_for_mail(  # noqa: PLR0913 - the two seams are what make this testable offline
    client: HubClient,
    agent: str,
    *,
    timeout: float,
    limit: int = 50,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> InboxPage:
    """Return unread mail for `agent`, blocking up to `timeout` seconds for some.

    Returns an empty list if the deadline passes with nothing there, and raises
    `Unreachable` if the hub stops answering, at whatever point that happens —
    always out of an `inbox` call, never out of the stream. See `_ticks`.

    `now` and `sleep` are injected together and must stay that way: a test that
    fakes the clock and not the sleep spends the deadline in real seconds, and a
    test that fakes the clock while the sleep advances nothing never terminates.
    Nothing but the tests passes either.

    **The predicate stays `page.messages`, never `page.unread`.** The page now
    carries the true backlog count, which makes `unread > 0` look like the more
    direct question and is the wrong one: this function's contract is that
    whatever ends the wait is handed straight to the caller to print and to
    acknowledge. A count is not something a caller can print, and stopping on one
    while the page is empty would report a wait as satisfied by mail nobody was
    given. `unread` rides along for the truncation line and for nothing else.

    It spends the whole deadline before giving up, and no more than it has to.
    Both halves are load-bearing: `cli.py` reports the number it was asked for,
    so a wait that returned early would put a duration in the transcript that
    never happened, and one that overran would be killed by a host cap the
    caller had deliberately stayed inside.
    """
    deadline = now() + timeout
    page = client.inbox(agent, limit=limit)
    if page.messages:
        return page
    while (remaining := deadline - now()) > MIN_STREAM_SECONDS:
        opened = now()
        rightsize = False
        # `closing`, because abandoning the generator holds the socket open and
        # the hub only notices at its next failing heartbeat write.
        with contextlib.closing(_ticks(client, agent, remaining)) as bells:
            for _chunk in bells:
                # A tick, not an event: the `hello` frame, a keep-alive, a real
                # bell and a stream that produced none of them are all the same
                # thing here — a reason to ask the hub what is true, and nothing
                # decoded. `hello` matters more than it looks. It arrives once the
                # subscription exists, so mail that landed in the gap between the
                # first read above and the subscription being registered is
                # picked up by the poll it triggers.
                page = client.inbox(agent, limit=limit)
                if page.messages or now() >= deadline:
                    return page
                if deadline - now() < remaining / 2:
                    # The socket timeout in force was sized for the deadline as
                    # it stood when this stream opened, and every read restarts
                    # it, so the last one outlives the deadline and the wait ends
                    # at the hub's next keep-alive rather than on time: measured
                    # against a live hub, `--wait 25` on a 20-second heartbeat
                    # returned in 40.0s, and the documented `--wait 90` took 100.
                    # Re-open with a right-sized one. Halving rather than
                    # re-opening on every tick, because the `hello` frame arrives
                    # with no time elapsed and a stricter test would reconnect on
                    # it forever.
                    rightsize = True
                    break
        if rightsize:
            continue
        floor = min(TICK_FLOOR - (now() - opened), deadline - now())
        if floor > 0:
            sleep(floor)
    if (left := deadline - now()) > 0:
        # Under `MIN_STREAM_SECONDS` there is no stream to spend it on, and the
        # deadline is still the deadline. Without this a `--wait 0.5` returned
        # instantly and stderr claimed half a second had passed.
        sleep(left)
    return client.inbox(agent, limit=limit)
