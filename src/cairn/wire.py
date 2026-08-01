"""The contract.

Everything cairn agrees on lives here: the message schema, the protocol version,
and the rules for turning both into JSON. This module imports nothing else from
cairn, and nothing else in cairn is allowed to define a wire shape.

That constraint is what makes the transport replaceable. Today the hub speaks
JSON over HTTP; if it ever speaks something else, only `hub.py` and `client.py`
change, because this file — not the transport — is the contract.

One rule is enforced structurally rather than by convention:

    A sender cannot claim its own message is verified.

`Message` has no `verified` field, so there is nothing for a sender to set and
nothing for a reader to be misled by. Verification is `Provenance`, which is
produced locally by whoever checked, is never deserialized from the wire, and
reports only what was actually checked. See docs/design.md, invariant I1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Self

PROTOCOL_VERSION = 1
"""Bumped on any incompatible change to the shapes below."""

BROADCAST = "*"
"""Recipient meaning "every registered agent except the sender"."""

MessageKind = Literal["tell", "ask", "reply"]
KINDS: tuple[MessageKind, ...] = ("tell", "ask", "reply")

MAX_BODY_CHARS = 16_000
"""Messages are prose between agents. Anything larger is an artifact (see `Artifact`)."""


def now() -> str:
    """Return the current time as an RFC 3339 string in UTC."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class WireError(ValueError):
    """A payload did not match the protocol."""


def _require(obj: dict[str, Any], key: str, kind: type) -> Any:  # noqa: ANN401
    value = obj.get(key)
    if not isinstance(value, kind):
        msg = f"field {key!r} must be {kind.__name__}, got {type(value).__name__}"
        raise WireError(msg)
    return value


@dataclass(frozen=True, slots=True)
class Artifact:
    """A pointer to something too large to be a message.

    Traces, waveforms, firmware images, datasets. cairn never moves these; it
    only says where they are, so that a 40 MB capture never becomes a
    message body.
    """

    host: str
    path: str
    sha256: str | None = None
    size_bytes: int | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the wire form."""
        return {"host": self.host, "path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the wire form."""
        return cls(
            host=_require(obj, "host", str),
            path=_require(obj, "path", str),
            sha256=obj.get("sha256"),
            size_bytes=obj.get("size_bytes"),
        )


@dataclass(frozen=True, slots=True)
class Agent:
    """A session that has joined the network.

    `name` is the address. It is chosen at registration and is unique across the
    whole network, so `bench/firmware` and `compute/analysis` are as addressable from
    each other as two processes on one box. cairn does not start, resume or stop
    the session behind a name — it only knows the name exists.
    """

    name: str
    machine: str
    cwd: str
    capabilities: tuple[str, ...] = ()
    session_id: str | None = None
    registered_at: str = field(default_factory=now)
    last_seen: str = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "name": self.name,
            "machine": self.machine,
            "cwd": self.cwd,
            "capabilities": list(self.capabilities),
            "session_id": self.session_id,
            "registered_at": self.registered_at,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the wire form."""
        return cls(
            name=_require(obj, "name", str),
            machine=_require(obj, "machine", str),
            cwd=_require(obj, "cwd", str),
            capabilities=tuple(obj.get("capabilities") or ()),
            session_id=obj.get("session_id"),
            registered_at=obj.get("registered_at") or now(),
            last_seen=obj.get("last_seen") or now(),
        )


@dataclass(frozen=True, slots=True)
class Message:
    """One message, as stored and as transmitted.

    Deliberately has no `verified`, no `trusted` and no `origin_is_human` field.
    A sender must not be able to assert its own trustworthiness; see `Provenance`.
    """

    seq: int
    kind: MessageKind
    sender: str
    recipient: str
    body: str
    correlation_id: str | None = None
    artifacts: tuple[Artifact, ...] = ()
    created_at: str = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "seq": self.seq,
            "kind": self.kind,
            "sender": self.sender,
            "recipient": self.recipient,
            "body": self.body,
            "correlation_id": self.correlation_id,
            "artifacts": [a.to_json() for a in self.artifacts],
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the wire form."""
        kind = _require(obj, "kind", str)
        if kind not in KINDS:
            msg = f"unknown message kind {kind!r}; expected one of {', '.join(KINDS)}"
            raise WireError(msg)
        body = _require(obj, "body", str)
        if len(body) > MAX_BODY_CHARS:
            msg = f"body is {len(body)} chars, limit is {MAX_BODY_CHARS}; send an artifact reference instead"
            raise WireError(msg)
        return cls(
            seq=int(obj.get("seq") or 0),
            kind=kind,  # type: ignore[arg-type]
            sender=_require(obj, "sender", str),
            recipient=_require(obj, "recipient", str),
            body=body,
            correlation_id=obj.get("correlation_id"),
            artifacts=tuple(Artifact.from_json(a) for a in obj.get("artifacts") or ()),
            created_at=obj.get("created_at") or now(),
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    """What *this process* actually checked about a message.

    Never sent, never parsed from JSON. Constructed only by the side that ran
    the check, and it reports the check that ran — including when that check was
    nothing at all. `unverified` is the honest default and must stay reachable:
    printing a reassuring word we did not earn is the specific failure this type
    exists to prevent.
    """

    verified: bool
    method: str
    detail: str = ""

    @classmethod
    def unverified(cls, detail: str = "no signature scheme configured") -> Self:
        """Return the honest default: nothing was checked."""
        return cls(verified=False, method="none", detail=detail)

    def label(self) -> str:
        """Return a short human- and agent-readable verdict."""
        return f"verified({self.method})" if self.verified else f"UNVERIFIED — {self.detail}"


@dataclass(frozen=True, slots=True)
class InboxEntry:
    """A message paired with the verification result computed locally for it."""

    message: Message
    provenance: Provenance

    def to_json(self) -> dict[str, Any]:
        """Return the rendering form.

        This is output, not wire input: `cairn inbox` prints it and no endpoint
        accepts it. Keeping provenance out of `Message` is what makes that
        asymmetry structural rather than a rule someone has to remember.
        """
        return {
            **self.message.to_json(),
            "provenance": {
                "verified": self.provenance.verified,
                "method": self.provenance.method,
                "detail": self.provenance.detail,
            },
        }


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload with the protocol version."""
    return {"v": PROTOCOL_VERSION, **payload}


def check_version(obj: dict[str, Any]) -> None:
    """Raise if the peer speaks a protocol this build does not understand."""
    version = obj.get("v")
    if version is None:
        return  # tolerated: pre-versioned payloads from a same-host dev build
    if version != PROTOCOL_VERSION:
        msg = f"peer speaks protocol v{version}, this build speaks v{PROTOCOL_VERSION}"
        raise WireError(msg)


def dumps(obj: dict[str, Any]) -> bytes:
    """Serialize a payload for the wire."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def loads(raw: bytes) -> dict[str, Any]:
    """Deserialize a payload from the wire, checking the protocol version."""
    try:
        obj = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"payload is not valid JSON: {exc}"
        raise WireError(msg) from exc
    if not isinstance(obj, dict):
        msg = f"payload must be a JSON object, got {type(obj).__name__}"
        raise WireError(msg)
    check_version(obj)
    return obj
