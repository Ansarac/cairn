"""The hub: one process, one SQLite file, JSON over HTTP.

Deliberately thin. Every handler parses a payload, calls one store method, and
serializes the result. There are no business rules here — if a rule ever wants
to live in this file, it belongs in `store.py` or in the caller instead, because
the moment behaviour leaks into the transport the transport stops being
replaceable.

stdlib `ThreadingHTTPServer` is not a fast web server. It does not need to be:
the traffic is a handful of agents exchanging sentences. What it buys is that
cairn installs with no dependencies at all, which matters on a hardware bench
where every package is one more thing that can break before a test run.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from cairn.errors import UsageError
from cairn.events import SSE_RETRY_MS, Notifier, heartbeat, sse_encode
from cairn.wire import Agent, Artifact, WireError, dumps, envelope, loads

if TYPE_CHECKING:
    from cairn.store import Store

MAX_REQUEST_BYTES = 1 << 20

HEARTBEAT_SECONDS = 20.0
"""How often an idle stream writes, so a departed reader is noticed and reaped."""


class _Handler(BaseHTTPRequestHandler):
    """Routes. One store call each."""

    protocol_version = "HTTP/1.1"
    server_version = "cairn"
    sys_version = ""
    store: Store  # bound by `make_server()`
    notifier: Notifier  # bound by `make_server()`

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence the default stderr access log; the hub is not a web server."""

    # -- plumbing -------------------------------------------------------------

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = dumps(envelope(payload))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            msg = f"request body is {length} bytes, limit is {MAX_REQUEST_BYTES}"
            raise WireError(msg)
        return loads(self.rfile.read(length)) if length else {}

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    @staticmethod
    def _int_param(query: dict[str, str], name: str, default: int) -> int:
        """Read a numeric query parameter, refusing a non-number as a refusal.

        `int(q.get("limit") or 50)` reads well and lands on the wrong exit code.
        A `ValueError` here falls to `_dispatch`'s catch-all, which answers 500,
        which `client._call` maps to `Unreachable` — exit 2, "the hub could not
        be reached", for a hub that is up and simply disagreed with an argument.
        That is a malformed request, which is 400 and exit 3.

        Only reachable by a hand-built request today, because argparse converts
        `--limit` before the client is called. It is fixed rather than left
        because cut 4 would otherwise have copied the wart onto a second route,
        and a wart on two routes is a pattern the next route will follow.
        """
        raw = query.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            msg = f"{name} must be a whole number, got {raw!r}"
            raise UsageError(msg) from exc

    def _dispatch(self, table: dict[str, Any]) -> None:
        route = urlparse(self.path).path
        handler = table.get(route)
        if handler is None:
            self._reply(404, {"error": f"no route {route}"})
            return
        try:
            handler()
        except (UsageError, WireError) as exc:
            self._reply(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the hub
            self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:
        """Handle the read routes."""
        self._dispatch(
            {
                "/v1/health": self._health,
                "/v1/peers": self._peers,
                "/v1/inbox": self._inbox,
                "/v1/sent": self._sent,
                "/v1/events": self._events,
                "/v1/notes": self._notes,
                "/v1/subjects": self._subjects,
            }
        )

    def do_POST(self) -> None:
        """Handle the write routes."""
        self._dispatch(
            {
                "/v1/register": self._register,
                "/v1/subjects": self._open_subject,
                "/v1/subjects/archive": self._archive_subject,
                "/v1/messages": self._send,
                "/v1/ack": self._ack,
                "/v1/notes": self._write_note,
                "/v1/notes/delete": self._delete_note,
            }
        )

    def _health(self) -> None:
        self._reply(200, {"ok": True, "agents": len(self.store.peers())})

    def _peers(self) -> None:
        exclude = self._query().get("exclude")
        self._reply(200, {"agents": [a.to_json() for a in self.store.peers(exclude=exclude)]})

    def _inbox(self) -> None:
        q = self._query()
        agent = q.get("agent")
        if not agent:
            msg = "inbox requires an ?agent= parameter"
            raise UsageError(msg)
        limit = self._int_param(q, "limit", 50)
        # `unread` and `head` ride alongside `messages`, which an older client
        # simply ignores. They are the whole of this route's change: the page was
        # never the problem, believing it was the backlog was. See wire.InboxPage.
        #
        # `since` rides back out again in the response, and that echo is load
        # bearing rather than symmetry. It is how a client learns whether the hub
        # it is talking to understood the window at all — an older one ignores the
        # parameter and answers with the oldest page of the whole backlog, which
        # is a different question answered in the same shape.
        self._reply(200, self.store.unread(agent, limit=limit, since=self._int_param(q, "since", 0)).to_json())

    def _sent(self) -> None:
        q = self._query()
        agent = q.get("agent")
        if not agent:
            msg = "sent requires an ?agent= parameter"
            raise UsageError(msg)
        messages, total = self.store.sent(agent, limit=self._int_param(q, "limit", 50))
        # `total` rides along where `/v1/inbox` sends none. A page that cannot be
        # told apart from a complete answer eventually gets treated as one; the
        # inbox's silence here is the known defect, not the precedent.
        self._reply(200, {"messages": [m.to_json() for m in messages], "total": total})

    def _register(self) -> None:
        # `Registration.to_json()` already nests the agent under "agent", so a
        # client that only reads that key keeps working across this addition.
        self._reply(200, self.store.register(Agent.from_json(self._read())).to_json())

    def _send(self) -> None:
        obj = self._read()
        message = self.store.append(
            kind=obj.get("kind", "tell"),
            sender=obj.get("sender", ""),
            recipient=obj.get("recipient", ""),
            body=obj.get("body", ""),
            correlation_id=obj.get("correlation_id"),
            artifacts=[Artifact.from_json(a) for a in obj.get("artifacts") or ()],
        )
        self._reply(200, {"message": message.to_json()})
        # Store first, ring second. If this process dies between the two, the
        # message is still durable and the recipient still gets it at its next
        # turn boundary — a lost bell costs latency, a lost message costs work.
        # The bell deliberately carries no body: see invariant I1.
        self.notifier.publish(message.recipient, {"seq": message.seq, "sender": message.sender, "kind": message.kind})

    def _events(self) -> None:
        """Stream bells for one agent until the client goes away.

        Long-lived, so it opts out of keep-alive and ends at connection close
        rather than announcing a length it cannot know.

        The heartbeat thread is not decoration. Without a periodic write the hub
        never learns that a reader has gone — the handler stays blocked in
        `__iter__`, its subscription is never closed, and it accumulates. The
        failing write is what tears the connection down.
        """
        agent = self._query().get("agent")
        if not agent:
            msg = "events requires an ?agent= parameter"
            raise UsageError(msg)

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        write_lock = threading.Lock()

        def emit(frame: bytes) -> bool:
            with write_lock, contextlib.suppress(OSError, ValueError):
                self.wfile.write(frame)
                self.wfile.flush()
                return True
            return False

        with self.notifier.subscribe(agent) as subscription:
            emit(sse_encode("hello", {"agent": agent, "retry_ms": SSE_RETRY_MS}))
            stop = threading.Event()

            def keepalive() -> None:
                while not stop.wait(HEARTBEAT_SECONDS):
                    if not emit(heartbeat()):
                        subscription.close()
                        return

            beat = threading.Thread(target=keepalive, daemon=True)
            beat.start()
            try:
                for payload in subscription:
                    if not emit(sse_encode("mail", payload)):
                        break
            finally:
                stop.set()

    def _ack(self) -> None:
        obj = self._read()
        cursor = self.store.ack(str(obj.get("agent", "")), int(obj.get("seq") or 0), rewind=bool(obj.get("rewind")))
        self._reply(200, {"cursor": cursor})

    def _write_note(self) -> None:
        obj = self._read()
        settles = obj.get("settles")
        supersedes = obj.get("supersedes")
        note = self.store.write_note(
            author=obj.get("author", ""),
            body=obj.get("body", ""),
            subject=obj.get("subject"),
            question=bool(obj.get("question")),
            settles=int(settles) if settles is not None else None,
            supersedes=int(supersedes) if supersedes is not None else None,
            artifacts=[Artifact.from_json(a) for a in obj.get("artifacts") or ()],
        )
        # The stored note goes back whole, which is what lets a client see that an
        # older hub dropped its `supersedes` on the floor rather than honouring it.
        self._reply(200, {"note": note.to_json()})
        # No `notifier.publish` here, and its absence is the design rather than an
        # omission. A note has no recipient to ring, and ringing everyone would
        # turn sediment into mail — the receiver decides when to look at a
        # subject, which is invariant I2. There is a test for this silence.

    def _delete_note(self) -> None:
        obj = self._read()
        note = self.store.delete_note(
            note_id=int(obj.get("id") or 0),
            author=str(obj.get("author", "")),
            reason=str(obj.get("reason", "")),
        )
        self._reply(200, {"note": note.to_json()})

    def _notes(self) -> None:
        q = self._query()
        entries, total, removed = self.store.notes(
            subject=q.get("subject"),
            open_only=q.get("open") == "1",
            find=q.get("find"),
            limit=self._int_param(q, "limit", 50),
            deleted=q.get("deleted") == "1",
        )
        # Serialized by hand rather than through `NoteEntry.to_json()`, because
        # that form carries a `provenance` block and the hub has no business
        # sending one: a trust verdict is worth exactly the check that produced
        # it, and no check ran here. See wire.NoteEntry and invariant I1.
        self._reply(
            200,
            {
                "notes": [
                    {"note": e.note.to_json(), "settled_by": e.settled_by, "superseded_by": e.superseded_by}
                    for e in entries
                ],
                "total": total,
                "removed": removed,
            },
        )

    def _subjects(self) -> None:
        archived = self._query().get("archived") == "1"
        self._reply(200, {"subjects": [s.to_json() for s in self.store.subjects(archived=archived)]})

    def _open_subject(self) -> None:
        obj = self._read()
        summary = self.store.create_subject(
            name=str(obj.get("name", "")),
            description=str(obj.get("description", "")),
            author=str(obj.get("author", "")),
        )
        self._reply(200, {"subject": summary.to_json()})
        # No `notifier.publish`, for the same reason `_write_note` sends none: a
        # subject has no recipient, and ringing everyone because somebody opened a
        # pile would turn sediment into mail. Invariant I2.

    def _archive_subject(self) -> None:
        obj = self._read()
        summary = self.store.archive_subject(
            name=str(obj.get("name", "")),
            author=str(obj.get("author", "")),
            reopen=bool(obj.get("reopen")),
        )
        self._reply(200, {"subject": summary.to_json()})


def make_server(
    store: Store, host: str = "127.0.0.1", port: int = 7777, notifier: Notifier | None = None
) -> ThreadingHTTPServer:
    """Build a hub bound to `host:port`, backed by `store`.

    The notifier is attached to the returned server as `server.notifier` so a
    caller — `serve()`, or a test — can `close_all()` it at shutdown and unblock
    every open stream.
    """
    bell = notifier or Notifier()
    handler = type("_BoundHandler", (_Handler,), {"store": store, "notifier": bell})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.notifier = bell  # type: ignore[attr-defined]
    return server


def stop_on_terminate(server: ThreadingHTTPServer) -> None:
    """Make a terminate signal shut the hub down instead of killing it.

    A hub that is not in a terminal is stopped by SIGTERM — `docker stop`,
    `systemctl stop`, a supervisor. Its default action ends the process where it
    stands, which is past `close_all()` and past `server_close()`: every open
    bell stream is dropped with no close frame, and the exit code is 143 rather
    than 0. SQLite's WAL survives that; nothing else about it is tidy. SIGINT
    needs no handler because it already arrives as `KeyboardInterrupt`.

    **`shutdown()` runs on a thread of its own, and that is not decoration.** It
    blocks until `serve_forever` has returned, and a handler runs on the thread
    that was interrupted — the same one sitting inside `serve_forever`. Calling
    it directly deadlocks the process it is trying to stop.

    Signals may only be installed from the main thread. Off it — a test, or an
    embedder — this does nothing rather than raising, because a hub that runs is
    worth more than a hub that exits neatly.
    """
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())


def serve(store: Store, host: str = "127.0.0.1", port: int = 7777) -> None:
    """Run a hub in the foreground until interrupted."""
    server = make_server(store, host, port)
    bound_host, bound_port = server.server_address[:2]
    # flush: this line is the only "I am up, on this port" signal there is, and
    # stdout is block-buffered whenever the hub is not on a tty — under systemd,
    # nohup or a pipe it would otherwise sit in the buffer until the process
    # exits, and be lost outright if the process is signalled.
    print(f"cairn hub on http://{bound_host}:{bound_port}", flush=True)
    stop_on_terminate(server)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.notifier.close_all()  # type: ignore[attr-defined] - unblock every open stream, or we never exit
    server.server_close()
