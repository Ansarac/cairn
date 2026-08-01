"""The only place that knows the hub is reachable over HTTP.

`cli.py` calls methods here and never touches a URL. That is the seam which
lets the transport change — to a unix socket, to SSE, to something else — without
the command surface noticing.

Every failure to reach the hub becomes `Unreachable` (exit code 2), and every
refusal by the hub becomes `UsageError` (exit code 3). Keeping those apart is
the difference between "nobody heard you" and "you asked for something that
does not exist".
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

from cairn.errors import Unreachable, UsageError
from cairn.wire import Agent, Artifact, Message, MessageKind, WireError, dumps, envelope, loads

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

DEFAULT_TIMEOUT = 10.0

HTTP_BAD_REQUEST = 400
"""The one status that means "you asked for something impossible", not "nobody heard you"."""

STREAM_TIMEOUT = 60.0
"""Socket timeout for a bell stream. Must stay well above the hub's heartbeat.

This is the client's dead-hub detector, and the mirror image of the heartbeat
itself: the hub writes periodically so it notices a reader that left, and the
reader expects those writes so it notices a hub that left. Set it below
`hub.HEARTBEAT_SECONDS` and every quiet stream tears itself down on a timer.
"""


class HubClient:
    """A thin, synchronous client for one hub."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Point this client at `base_url`, giving up after `timeout` seconds."""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- plumbing -------------------------------------------------------------

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None, **query: Any) -> dict[str, Any]:  # noqa: ANN401
        url = f"{self.base_url}{path}"
        clean = {k: str(v) for k, v in query.items() if v is not None}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"
        data = dumps(envelope(payload)) if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - scheme is ours, from config
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc.read())
            if exc.code == HTTP_BAD_REQUEST:
                raise UsageError(detail) from exc
            msg = f"hub returned {exc.code}: {detail}"
            raise Unreachable(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"cannot reach hub at {self.base_url}: {exc}"
            raise Unreachable(msg) from exc
        except WireError as exc:
            msg = f"hub spoke something unexpected: {exc}"
            raise Unreachable(msg) from exc

    # -- calls ----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return the hub's health payload."""
        return self._call("GET", "/v1/health")

    def register(self, agent: Agent) -> Agent:
        """Join the network, or refresh an existing registration."""
        return Agent.from_json(self._call("POST", "/v1/register", agent.to_json())["agent"])

    def peers(self, exclude: str | None = None) -> list[Agent]:
        """List registered agents."""
        return [Agent.from_json(a) for a in self._call("GET", "/v1/peers", exclude=exclude)["agents"]]

    def send(  # noqa: PLR0913, PLR0917 - these six are the message schema; collapsing them would hide the contract
        self,
        kind: MessageKind,
        sender: str,
        recipient: str,
        body: str,
        correlation_id: str | None = None,
        artifacts: Sequence[Artifact] = (),
    ) -> Message:
        """Post one message and return it with its assigned sequence."""
        payload = {
            "kind": kind,
            "sender": sender,
            "recipient": recipient,
            "body": body,
            "correlation_id": correlation_id,
            "artifacts": [a.to_json() for a in artifacts],
        }
        return Message.from_json(self._call("POST", "/v1/messages", payload)["message"])

    def inbox(self, agent: str, limit: int = 50) -> list[Message]:
        """Fetch unread messages for `agent`, oldest first."""
        return [Message.from_json(m) for m in self._call("GET", "/v1/inbox", agent=agent, limit=limit)["messages"]]

    def ack(self, agent: str, seq: int) -> int:
        """Advance the agent's cursor and return where it now sits."""
        return int(self._call("POST", "/v1/ack", {"agent": agent, "seq": seq})["cursor"])

    def stream(self, agent: str, chunk_size: int = 4096, timeout: float = STREAM_TIMEOUT) -> Iterator[bytes]:
        """Open the bell stream for `agent` and yield raw bytes until it ends.

        Deliberately not part of `_call`: this connection is meant to stay open
        for hours, so it does not inherit the request timeout — `STREAM_TIMEOUT`
        applies instead — and its body is a byte stream rather than one JSON
        object. Decoding is `events.sse_decode`'s job; this method moves bytes.

        The caller is expected to reconnect. Anything that ends the stream —
        a hub restart, a proxy idle-timeout, a laptop lid — surfaces as the
        iterator simply finishing, because a bell stream going quiet is not an
        error condition. It costs latency, never a message: the inbox on the hub
        is the source of truth and a reconnect re-reads it.

        `read1`, not `read`. `HTTPResponse.read(n)` blocks until it has all `n`
        bytes or the connection closes, so with a 4 KiB buffer a bell of sixty
        bytes would sit unseen until roughly seventy more arrived — which, on a
        quiet network, is never. `read1` returns whatever one underlying read
        produced. This was measured: `curl -N` saw both frames immediately while
        the `read`-based client saw nothing at all.
        """
        url = f"{self.base_url}/v1/events?{urllib.parse.urlencode({'agent': agent})}"
        request = urllib.request.Request(url, method="GET")  # noqa: S310 - scheme is ours, from config
        request.add_header("Accept", "text/event-stream")
        try:
            response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            # HTTPError subclasses URLError, so it has to be caught first or a
            # "you asked for something impossible" turns into "nobody heard you".
            detail = _error_detail(exc.read())
            if exc.code == HTTP_BAD_REQUEST:
                raise UsageError(detail) from exc
            msg = f"hub returned {exc.code} on the bell stream: {detail}"
            raise Unreachable(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"cannot open the bell stream at {self.base_url}: {exc}"
            raise Unreachable(msg) from exc
        with response:
            while True:
                try:
                    chunk = response.read1(chunk_size)
                except (TimeoutError, OSError):
                    return
                if not chunk:
                    return
                yield chunk


def _error_detail(raw: bytes) -> str:
    try:
        return str(loads(raw).get("error") or raw.decode())
    except (WireError, UnicodeDecodeError):
        return raw.decode(errors="replace")
