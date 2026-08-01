"""The bell, fanned out in-process, plus the SSE codec that carries it over the wire.

A hub request thread calls `Notifier.publish` when it has stored a message; an
SSE response thread iterates a `Subscription` and writes `sse_encode` frames
down a socket to a nudger. That is the whole module.

**This stream carries a bell, never content.** The inbox in the hub's SQLite
file is the only source of truth about what mail exists. A frame here says
"there is something for you" and nothing more — invariant I2 in
docs/design.md, and the reason §5's idle-session nudge is allowed to be one
typed line rather than a delivery mechanism.

That single property is what licenses everything else in here to be simple:

    The stream is allowed to drop events, reorder them, or miss them entirely
    across a reconnect. A dropped bell costs latency, never a message.

It costs only latency because the recipient re-reads the whole truth anyway:
the next bell, or the next turn-boundary hook, causes a full `cairn inbox`,
which returns the whole unread list with a server-side cursor behind it.
Nothing here is ever the last copy of anything.

So the per-subscriber queues are bounded and they **drop and mark** — they
never block, and never grow. `Subscription.dropped()` reports the count so the
loss is visible rather than silent.

If you are reading this because the dropping looks like a bug: it is not, and
"fixing" it is how you get a deadlock. `publish` runs on the thread that is
part-way through serving somebody else's `POST /v1/messages`. Make it wait on a
queue and one wedged SSE reader — a nudger on a laptop that closed its lid,
a TCP connection that will not drain — stalls message delivery for every other
agent on the network. A subscriber's health must never be able to reach back
into the hub. Drop the bell instead; the inbox still has the message.

Dependency direction: `hub → events → wire`. This module knows nothing about
HTTP, sockets, or the store.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections import deque
from collections.abc import Iterable, Iterator
from typing import Any, Self

from cairn.wire import BROADCAST, dumps

SSE_RETRY_MS: int = 3000
"""Reconnect delay to advertise to clients, in milliseconds, via a `retry:` line.

Three seconds is chosen against the failure it exists for: the hub restarting.
Fast enough that a bell missed during the restart is late by seconds rather than
minutes, slow enough that a hub that is genuinely down is not being hammered by
every nudger on the network.
"""


def sse_encode(event: str, data: dict[str, Any]) -> bytes:
    """Return one SSE frame: an `event:` line, `data:` line(s) of compact JSON, a blank line."""
    if "\n" in event or "\r" in event:
        msg = f"SSE event name may not contain a line break: {event!r}"
        raise ValueError(msg)
    # Compact JSON never contains a raw newline, so this is one `data:` line in
    # practice; the split keeps the encoder correct if that ever stops being true.
    lines = [f"event: {event}"]
    lines += [f"data: {line}" for line in dumps(data).decode().split("\n")]
    return ("\n".join(lines) + "\n\n").encode()


def heartbeat() -> bytes:
    """Return an SSE comment frame: bytes on the wire, no event for the reader.

    An idle bell stream can go quiet for hours, which is long enough for a
    proxy, a NAT table or an over-helpful VPN to reap the connection without
    telling either end. Writing this occasionally proves the socket is alive.

    Deliberately a plain function and not a thread: whoever owns the socket owns
    the schedule, and this module owns no threads at all.
    """
    return b": keep-alive\n\n"


def sse_decode(chunks: Iterable[bytes]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse a byte stream of SSE frames into (event, data) pairs.

    Tolerant by construction, because the input is a socket read loop: chunks
    may split anywhere (mid-line, or between the CR and the LF of a CRLF),
    lines may end LF or CRLF, `:` comment lines are heartbeats and are skipped,
    and fields this build does not know (`id`, `retry`, whatever a later hub
    adds) are ignored rather than fatal.

    Two things are dropped silently rather than raised on, for the reason in the
    module docstring — a malformed bell is worth less than the connection:
    a frame whose data is not a JSON object, and a trailing frame that the
    stream ended before terminating with a blank line.
    """
    buffer = b""
    event = ""
    data: list[str] = []
    for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            line = raw.removesuffix(b"\r").decode("utf-8", "replace")
            if not line:
                frame = _frame(event, data)
                event, data = "", []
                if frame is not None:
                    yield frame
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)


def _frame(event: str, data: list[str]) -> tuple[str, dict[str, Any]] | None:
    """Turn accumulated field values into one event, or None if there is nothing usable."""
    if not data:
        return None
    try:
        parsed = json.loads("\n".join(data))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return (event or "message", parsed)


class Subscription:
    """One reader's bell stream, with a bounded buffer that drops rather than waits.

    `agent` is the name whose mail this stream is about. Iterating blocks until
    a bell arrives; `close()` ends the iteration.

    The buffer is small on purpose. It exists to absorb a reader that is a few
    milliseconds behind, not to be a queue — if it fills, the excess is counted
    by `dropped()` and thrown away, because the alternative is making a hub
    thread wait on this reader. See the module docstring.
    """

    def __init__(self, agent: str, notifier: Notifier, queue_size: int) -> None:
        """Build a stream for `agent` owned by `notifier`; call `Notifier.subscribe` instead of this."""
        self.agent = agent
        self._notifier = notifier
        self._queue_size = queue_size
        self._events: deque[dict[str, Any]] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._dropped = 0

    def __enter__(self) -> Self:
        """Return this subscription; leaving the block unsubscribes it."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Unsubscribe and close, however the block ended."""
        self._notifier.unsubscribe(self)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Yield bell payloads as they arrive, blocking in between.

        Ends — StopIteration, not a hang — once `close()` has been called and
        anything already buffered has been handed over.

        The lock is released before each yield, so a reader that takes its time
        writing a frame to a socket cannot hold up a publisher.
        """
        while True:
            with self._cond:
                while not self._events and not self._closed:
                    self._cond.wait()
                if not self._events:
                    return
                payload = self._events.popleft()
            yield payload

    @property
    def closed(self) -> bool:
        """Return True once this stream has been closed."""
        with self._cond:
            return self._closed

    def close(self) -> None:
        """End the stream and wake a reader blocked in `__iter__`. Idempotent."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def dropped(self) -> int:
        """Return how many bells were discarded because this buffer was full.

        Non-zero means this reader fell behind, not that mail was lost: every
        dropped bell describes an inbox that is still sitting in the hub.
        """
        with self._cond:
            return self._dropped

    def _offer(self, payload: dict[str, Any]) -> None:
        """Buffer `payload`, or count it as dropped. Never blocks on the reader."""
        with self._cond:
            if self._closed:
                return
            if len(self._events) >= self._queue_size:
                # Refuse the newest rather than evicting the oldest. Either is
                # defensible and neither loses a message; this one is obvious.
                self._dropped += 1
                return
            self._events.append(payload)
            self._cond.notify()


class Notifier:
    """Fan-out from the hub's request threads to whatever is streaming SSE.

    Thread-safe: `publish` is called from a `ThreadingHTTPServer` request
    thread while other threads are blocked in `Subscription.__iter__`.

    Subscribers are keyed by exact agent name. `BROADCAST` is a property of a
    *recipient*, not of a subscription: publishing to it reaches everyone,
    while subscribing to it only asks for mail addressed literally to `"*"`.

    **One subscription covers one name, so a machine hosting several registered
    sessions opens one stream per session.** That is a deliberate limit, not an
    oversight. A multiplexed subscription would need the payload to say which
    name each bell is about, and it would need the reader to merge several
    blocking iterators onto one socket — real complexity, bought for a case that
    is two or three sessions on a developer's machine. If that number ever
    climbs, the change is `subscribe(*agents)` plus stamping the recipient into
    the payload, and nothing above this class has to move.
    """

    def __init__(self, queue_size: int = 32) -> None:
        """Build a notifier whose subscribers each buffer at most `queue_size` bells."""
        if queue_size < 1:
            msg = f"queue_size must be at least 1, got {queue_size}"
            raise ValueError(msg)
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Subscription]] = {}

    def subscribe(self, agent: str) -> Subscription:
        """Register a bell stream for `agent` and return it.

        The result is also a context manager, which is the safe way to use it:
        leaving the block unsubscribes even if the connection died mid-frame.
        """
        sub = Subscription(agent, self, self._queue_size)
        with self._lock:
            self._subscribers.setdefault(agent, []).append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Forget `sub` and close it. Safe to call twice, and on a stranger."""
        with self._lock:
            peers = self._subscribers.get(sub.agent)
            if peers is not None:
                remaining = [s for s in peers if s is not sub]
                if remaining:
                    self._subscribers[sub.agent] = remaining
                else:
                    del self._subscribers[sub.agent]
        sub.close()

    def publish(self, recipient: str, payload: dict[str, Any]) -> None:
        """Ring the bell for `recipient`, or for everyone when it is `BROADCAST`.

        Returns immediately and raises nothing on account of a subscriber:
        the caller is a hub thread part-way through storing a message, and a
        reader that has stopped reading must not be able to reach it.

        `payload` is copied once and then shared between subscribers; treat it
        as read-only on both sides.
        """
        bell = dict(payload)
        with self._lock:
            self._forget_closed()
            if recipient == BROADCAST:
                targets = [s for subs in self._subscribers.values() for s in subs]
            else:
                targets = list(self._subscribers.get(recipient, ()))
        for sub in targets:
            # Nothing a subscriber can do may propagate back into the hub thread.
            with contextlib.suppress(Exception):
                sub._offer(bell)  # noqa: SLF001 - Notifier and Subscription are one mechanism under two names

    def subscriber_count(self, agent: str | None = None) -> int:
        """Count live subscribers: for `agent`, or across every agent when it is None."""
        with self._lock:
            self._forget_closed()
            if agent is None:
                return sum(len(subs) for subs in self._subscribers.values())
            return len(self._subscribers.get(agent, ()))

    def close_all(self) -> None:
        """End every open stream and forget them. For hub shutdown.

        Without this, `serve_forever()` returning leaves each SSE handler thread
        blocked in `Subscription.__iter__` with nothing left to wake it, and the
        process does not exit. Idempotent, and safe to call while publishers are
        still running: they will simply find no targets.
        """
        with self._lock:
            targets = [s for subs in self._subscribers.values() for s in subs]
            self._subscribers.clear()
        for sub in targets:
            sub.close()

    def _forget_closed(self) -> None:
        """Drop subscriptions that closed without unsubscribing. Caller holds the lock."""
        for agent, subs in list(self._subscribers.items()):
            live = [s for s in subs if not s.closed]
            if live:
                self._subscribers[agent] = live
            else:
                del self._subscribers[agent]
