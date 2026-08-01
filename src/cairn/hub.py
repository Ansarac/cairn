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
                "/v1/events": self._events,
            }
        )

    def do_POST(self) -> None:
        """Handle the write routes."""
        self._dispatch({"/v1/register": self._register, "/v1/messages": self._send, "/v1/ack": self._ack})

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
        limit = int(q.get("limit") or 50)
        self._reply(200, {"messages": [m.to_json() for m in self.store.unread(agent, limit=limit)]})

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
        cursor = self.store.ack(
            str(obj.get("agent", "")), int(obj.get("seq") or 0), rewind=bool(obj.get("rewind"))
        )
        self._reply(200, {"cursor": cursor})


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


def serve(store: Store, host: str = "127.0.0.1", port: int = 7777) -> None:
    """Run a hub in the foreground until interrupted."""
    server = make_server(store, host, port)
    bound_host, bound_port = server.server_address[:2]
    # flush: this line is the only "I am up, on this port" signal there is, and
    # stdout is block-buffered whenever the hub is not on a tty — under systemd,
    # nohup or a pipe it would otherwise sit in the buffer until the process
    # exits, and be lost outright if the process is signalled.
    print(f"cairn hub on http://{bound_host}:{bound_port}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.notifier.close_all()  # type: ignore[attr-defined] - unblock every open stream, or we never exit
    server.server_close()
