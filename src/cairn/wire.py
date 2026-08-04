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
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Self

PROTOCOL_VERSION = 1
"""Bumped on any incompatible change to the shapes below.

**Incompatible**, and the word is doing real work, because `check_version`
compares for equality rather than ordering. Bumping this does not deprecate an
old peer; it disconnects one. A v2 client and a v1 hub cannot exchange a `tell`
or an `inbox` either — every route fails, not just the new one.

So a purely *additive* change does not bump, and cut 4 is the worked example:
`Note` and its routes are new shapes at new paths. An old hub answers the new
route with 404, which `client._call` maps to `Unreachable`; a new hub is
unchanged for an old client, which never calls them. Nothing that worked before
stops working, and every reader of the two builds still agrees on what a
`Message` is. Bumping here would have broken messaging between two builds that
disagree about nothing.

What *does* bump: changing or removing a field on an existing shape, changing
what a field means, or making an old payload parse differently. If you cannot
say which existing exchange breaks without the bump, you probably do not need
one — and if you do bump, both ends have to be upgraded together, so say so.

**Second worked example, and the harder one: `Message.signature` did not bump.**
Cut 4 added new shapes at new paths, which is the easy case. This added a field
to the most-used existing shape, which is the case a reader will be least sure
about — so the test was applied out loud rather than assumed. New client, old
hub: the extra key is ignored on the way in, nothing is stored, and the message
comes back unsigned, so the sender's own `cairn sent` reads `UNVERIFIED` — the
answer that surface gave for every build before this one. Old client, new hub:
`from_json` reads with `obj.get` and never looks for the key. Neither direction
loses an exchange that worked; one of them loses a feature it never had, which
is not the same thing. Bumping would have disconnected every peer to deliver a
verdict that only ever concerns the machine reading it.
"""

BROADCAST = "*"
"""Recipient meaning "every registered agent except the sender"."""

MessageKind = Literal["tell", "ask", "reply"]
KINDS: tuple[MessageKind, ...] = ("tell", "ask", "reply")

MAX_BODY_CHARS = 16_000
"""Messages are prose between agents. Anything larger is an artifact (see `Artifact`)."""

MAX_SUBJECT_CHARS = 120
"""A subject is an address for a pile of notes, not a sentence."""

_SUBJECT = re.compile(r"[a-z0-9][a-z0-9._/-]*")
"""What a subject may contain. Narrow on purpose; see `normalize_subject`."""


def now() -> str:
    """Return the current time as an RFC 3339 string in UTC."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_subject(raw: str) -> str:
    """Return the canonical form of a note subject, or raise.

    Two rules, and both are load-bearing rather than tidiness.

    **It folds case.** A subject is how a note is found months later by somebody
    who was not there when it was written. If `rig-a` and `Rig-A` are two piles,
    the reader finds one of them and has no way to learn the other exists — a
    silent failure that destroys the only thing notes are for. The fold happens
    here, and every command that takes a subject prints the one it actually used
    so the normalization is never a surprise.

    **The character set excludes whitespace entirely.** A subject is
    peer-authored text that `cairn notes` prints in its own column-zero header
    line, so a subject containing a newline could forge a header exactly as a
    message body could. `render` indents bodies for that reason; subjects cannot
    be indented, so they are constrained instead. There is a test.
    """
    subject = raw.strip().lower()
    if not subject:
        msg = "a note needs a subject: the rig, run or board it is about"
        raise WireError(msg)
    if len(subject) > MAX_SUBJECT_CHARS:
        msg = f"subject is {len(subject)} chars, limit is {MAX_SUBJECT_CHARS}; a subject is an address, not a sentence"
        raise WireError(msg)
    if not _SUBJECT.fullmatch(subject):
        msg = (
            f"subject {subject!r} may contain only a-z, 0-9 and . _ - / and must start with a letter or digit; "
            f"try something like 'rig-a' or 'eval-441'"
        )
        raise WireError(msg)
    return subject


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

    `retracted_at` is the sender withdrawing mail that is **still in the pipe**.
    It never reaches a recipient — `store.unread` filters it out entirely — so the
    only surface it appears on is the sender's own `cairn sent`, where a row that
    was withdrawn must not read as one that was delivered. The body is kept, and
    that is the difference from `Note.deleted_at`: deleting a note is about text
    that should not exist, while retracting is about delivery that should not
    happen, and the sender is owed a record of what it pulled back.
    """

    seq: int
    kind: MessageKind
    sender: str
    recipient: str
    body: str
    correlation_id: str | None = None
    artifacts: tuple[Artifact, ...] = ()
    created_at: str = field(default_factory=now)
    retracted_at: str = ""
    signature: str = ""
    """Evidence, not a verdict — which is the distinction that keeps I1 intact.

    `test_a_sender_cannot_claim_its_own_message_is_verified` forbids `verified`,
    `verified_by`, `trusted`, `signature_ok` and `origin_is_human`, and the
    absence of `signature` from that list is a decision the test's author already
    made rather than an omission this field slipped through. A sender asserting
    *"I am trustworthy"* is unfalsifiable and is what I1 exists to prevent. A
    sender attaching bytes that a reader's own key either reproduces or does not
    is the opposite: it is a claim that can fail, and `provenance` is still the
    only thing allowed to say whether it did.

    Empty on anything sent before docs/design.md §12 item 9's first cut, and on
    anything that crossed a hub too old to store it. Empty means *nobody signed
    this*, which is not the same finding as a signature that does not check out —
    see `Provenance.mismatch`.

    What it covers is decided by `signing.canonical`, and it is not everything:
    the hub assigns `seq` and `created_at`, so neither is inside it.
    """

    @property
    def retracted(self) -> bool:
        """Return whether the sender pulled this back before it was read."""
        return bool(self.retracted_at)

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
            "retracted_at": self.retracted_at,
            "signature": self.signature,
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
            retracted_at=str(obj.get("retracted_at") or ""),
            signature=str(obj.get("signature") or ""),
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

    @classmethod
    def mismatch(cls, method: str, detail: str) -> Self:
        """Return the verdict for a signature that was checked and did not match.

        A third state, because two were hiding a distinction that matters more
        than the one they drew. `unverified` means *nothing was checked* — no
        scheme, or no signature on this row — and it is evidence of nothing.
        This means a check ran and **failed**, which is evidence of something.
        Flattening them gives the row that should be loudest the same word as
        the row that is merely ordinary.

        It is deliberately not called forgery. The common cause is benign and
        `provenance.assess_sent` puts it in the detail: a name taken over from
        another working directory is signed by a key this one does not hold, so
        the predecessor's sends cannot verify here and should not. cairn reports
        the check it ran; what the failure means is the reader's to decide, which
        is I3 with a hash attached.
        """
        return cls(verified=False, method=method, detail=detail)

    def token(self) -> str:
        """Return the verdict alone, with no explanation attached.

        This is what rides every message. The explanation is worth saying once
        per reading, not once per message — but the verdict itself has to sit
        next to the content it describes, so `UNVERIFIED` stays shouted.

        `MISMATCH` is shouted too and is a *different word* rather than a
        qualifier on this one, because the reader this is written for is
        skimming: `UNVERIFIED (mismatch)` shares its first and longest token
        with the line that means nothing happened, on a surface where every line
        said exactly that for every build until now.
        """
        if self.verified:
            return f"verified({self.method})"
        return "UNVERIFIED" if self.method == "none" else "MISMATCH"

    def label(self) -> str:
        """Return the verdict and why, for somewhere it is said only once.

        Built from `token` rather than from the literal it used to hard-code, so
        that a `MISMATCH` row's footnote says `MISMATCH — …` and not
        `UNVERIFIED — …`. The old form spelled the word out, which is exactly how
        a new verdict gets rendered under the old one's name in the one place
        that explains it.

        **A pass has an explanation too, and dropping it was the old shape's
        other assumption.** While nothing could verify, a verified label had
        nothing to say and returned the bare verdict. Now that one thing can, the
        detail is where the *limit* of that pass lives — a signature covers the
        words and the addressee and not the sequence or the time — and that is
        the last sentence a surface built to avoid overclaiming should throw
        away.
        """
        return f"{self.token()} — {self.detail}" if self.detail else self.token()


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


@dataclass(frozen=True, slots=True)
class InboxPage:
    """A page of unread mail, plus the facts a page cannot carry by itself.

    `messages` is capped by the caller's `--limit`. `unread` and `head` are not:
    they are `COUNT(*)` and `MAX(seq)` over the whole backlog behind that page.
    The distinction is the entire point of this shape, and it exists because
    inferring either one from the page is wrong in a way nothing reports.

    **`head` is what un-deafens the bell.** `cairn bell` latches on the highest
    seq it has announced, so that a reader who chose not to open the inbox gets a
    reminder rather than a loop. Computed off a capped page that head stops
    advancing the moment the backlog passes the cap — the latch pins to it, and
    every later turn boundary compares an unmoved head against an equal latch and
    stays silent. Permanently, until the reader drains by hand. `nudge` built its
    counter the same way, so the wake path went quiet alongside the hook.

    **`unread` is what stops a full page reading as a complete answer.** The same
    rule `notes` and the sent log already ship with, arriving late on the surface
    that taught it: a caller who cannot tell those two apart will eventually treat
    one as the other.

    **`since`, `matching` and `floor` describe a window, and they are three
    because the reader needs three different numbers to be told the truth.**
    `--since` moves the bottom of a read forward without moving the read position,
    so a reader working through a backlog can ask for the part it has not looked
    at yet. `unread` still counts the whole backlog, because that is what the word
    means and because the bell reads it. `matching` counts what is past the
    window as well, and it is what truncation must be measured against — against
    `unread` a windowed page reports itself as cut short by a `--limit` that is
    not what dropped those rows. `floor` is where the page actually began, which
    is `max(read position, since)`, and it is the only thing that can explain a
    window the reader asked for and did not get: a `--since` behind the read
    position shows fewer rows than were asked for, and nothing else in the
    response says why. Named for what it is rather than for the read position it
    equals on an unwindowed read, because on a windowed one it frequently is not
    that.

    **This does not bump `PROTOCOL_VERSION`, and the question was asked properly
    rather than waved through.** `check_version` compares for equality, so a bump
    disconnects an old peer on every route rather than deprecating it. Every key
    here is additive on the `/v1/inbox` response and no existing field changes
    meaning: an old client ignores them and behaves exactly as it does today.

    **What differs between the two generations of key is what their absence
    costs, and it is the reason `client.inbox` refuses where it used to fall
    back.** A hub that does not send `unread` and `head` withholds two numbers the
    caller can live without — the fallback reproduces exactly the behaviour that
    shipped for three cuts, deafness included, which is honest degradation. A hub
    that does not understand `?since=` withholds nothing and *answers a different
    question*: it returns the oldest page of the whole backlog, which a caller
    that asked for a window would print as the window. That is not a number two
    ends need not agree on; it is mail shown in the wrong place, or hidden. So the
    hub echoes the window it applied, and a client that asked for one and is not
    told it was applied stops rather than guesses. `since` is echoed **as it was
    asked for**, not as the bound that bit, so that the echo answers "did you
    understand me" and `floor` separately answers "what did you actually do".
    """

    messages: tuple[Message, ...] = ()
    unread: int = 0
    head: int = 0
    floor: int = 0
    since: int = 0
    matching: int | None = None
    """How many are past the window as well as past the read position.

    `None` means no separate count was reported, which covers both a page with no
    window and a hub built before there was one. Resolved by `available`; never
    defaulted to zero, because a zero here is a real answer — a window past the
    end of the backlog matches nothing — and the two must not look alike.
    """

    @property
    def available(self) -> int:
        """Return how many rows this read could show if `--limit` alone were raised."""
        return self.unread if self.matching is None else self.matching

    @property
    def truncated(self) -> bool:
        """Return whether more is reachable by this same read than the page shows."""
        return self.available > len(self.messages)

    def to_json(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "messages": [m.to_json() for m in self.messages],
            "unread": self.unread,
            "head": self.head,
            "floor": self.floor,
            "since": self.since,
            "matching": self.matching,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the wire form, deriving what an older hub does not send.

        `is None` rather than a falsy test, deliberately: a new hub reporting an
        empty inbox sends `unread: 0`, and reading that as "absent" would send the
        caller down the fallback path on the one answer where it least matters and
        most confuses anyone debugging it. `matching` keeps its `None` rather than
        being derived, because "this hub told me nothing about a window" is the
        signal `client.inbox` checks.
        """
        messages = tuple(Message.from_json(m) for m in obj.get("messages") or ())
        unread = obj.get("unread")
        head = obj.get("head")
        matching = obj.get("matching")
        return cls(
            messages=messages,
            unread=len(messages) if unread is None else int(unread),
            head=max((m.seq for m in messages), default=0) if head is None else int(head),
            floor=int(obj.get("floor") or 0),
            since=int(obj.get("since") or 0),
            matching=None if matching is None else int(matching),
        )


@dataclass(frozen=True, slots=True)
class SentEntry:
    """A message this session sent, paired with what was verified about the record of it.

    Not `InboxEntry`, and the difference is not cosmetic. An inbox entry is
    somebody else's words arriving; this is the hub's account of **your own**,
    handed back over a connection cairn does not authenticate. The verdict
    therefore qualifies a different thing — on the inbox `UNVERIFIED` means "we
    cannot prove who sent this", here it means "we cannot prove you sent this" —
    which is why `provenance.assess_sent` is its own seam and why the two
    renderers frame differently.

    Keeping the type separate is what makes that structural. `InboxEntry.to_json`
    says in its own docstring that it is what `cairn inbox` prints; reusing it
    here would quietly make that false, and a reader comparing two `--json`
    outputs would have no way to tell which surface they were looking at.
    """

    message: Message
    provenance: Provenance = field(default_factory=Provenance.unverified)

    def to_json(self) -> dict[str, Any]:
        """Return the rendering form.

        Output, not wire input, on the same terms as `InboxEntry`: `cairn sent`
        prints it and no endpoint accepts it. The hub serializes plain `Message`
        rows and never emits a provenance key, so nothing can arrive claiming a
        verdict this process did not reach.
        """
        return {
            **self.message.to_json(),
            "provenance": {
                "verified": self.provenance.verified,
                "method": self.provenance.method,
                "detail": self.provenance.detail,
            },
        }


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """What retracting a message actually managed to do.

    Output only. It exists because a retraction on a broadcast is **partial by
    nature** — one row, many mailboxes, each with its own cursor — so "it worked"
    and "it failed" are both wrong answers. `withheld` is how many mailboxes will
    now never see it and `read_by` names the ones that already had it, because a
    sender who has just failed to unsay something needs to know exactly who heard.

    **`withheld_from` names the other half, and it is not symmetry for its own
    sake.** The sender's next question is always *who still holds this* — and for
    two cuts the answer was a bare count, so an acceptance session recovered it by
    subtracting the named failures from a `cairn peers` snapshot taken moments
    earlier. That worked with two peers and it was already wrong: a third peer had
    registered in between, and the arithmetic could not see it. The names are
    right there in `store.retract`; withholding them made the caller reconstruct
    them badly.
    """

    message: Message
    withheld: int = 0
    read_by: tuple[str, ...] = ()
    withheld_from: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Return the rendering form."""
        return {
            "message": self.message.to_json(),
            "withheld": self.withheld,
            "read_by": list(self.read_by),
            "withheld_from": list(self.withheld_from),
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the rendering form.

        `withheld_from` is absent from an older hub, which is why `withheld` stays
        the count of record rather than becoming `len(withheld_from)`. A caller
        that derived the count from the list would report zero spared mailboxes
        against a hub that spared several — the failure mode this project keeps
        naming, where the absence of an additive field is read as a fact.
        """
        return cls(
            message=Message.from_json(_require(obj, "message", dict)),
            withheld=int(obj.get("withheld") or 0),
            read_by=tuple(str(name) for name in obj.get("read_by") or ()),
            withheld_from=tuple(str(name) for name in obj.get("withheld_from") or ()),
        )


@dataclass(frozen=True, slots=True)
class Note:
    """One piece of sediment: something worth knowing that outlives its session.

    A `Message` is addressed to a session and read once. A `Note` is addressed to
    a **subject** — a rig, a run, a board — and waits there for whoever turns up
    next, who may be nobody that was present when it was written. That is the
    whole difference, and it is why notes are not messages with a longer shelf
    life: there is no recipient, no cursor, no bell, and reading one consumes
    nothing.

    Like `Message`, it carries no field asserting its own trustworthiness.

    `question` marks a note that is not settled knowledge but an open loop, and
    `settles` is how a later note closes one. Whether a question is still open is
    therefore **derived** — it is open while no note points at it — rather than a
    column somebody has to remember to update. Deriving it is what stops it
    drifting; append-only is what keeps the record of who thought what, when.

    `supersedes` is the same shape for statements that `settles` is for questions:
    a later note saying *this replaced that*. Whether a note has been superseded is
    derived too, and for the same reason. It is a second relation rather than a
    reuse of `settles`, because settling closes a loop and superseding replaces a
    fact — folding them would make `open` mean two things on the one field whose
    entire value is that it means one.

    `deleted_at` is the exception to append-only, and it is bounded rather than
    general. The row survives, keeping its id so that anything pointing at it still
    resolves; the body does not, because the reason to reach for this is sometimes
    that the body should never have been written down. What replaces it is why it
    went and who took it out. See `store.delete_note`.
    """

    id: int
    subject: str
    author: str
    body: str
    question: bool = False
    settles: int | None = None
    supersedes: int | None = None
    artifacts: tuple[Artifact, ...] = ()
    created_at: str = field(default_factory=now)
    deleted_at: str = ""
    deleted_by: str = ""

    @property
    def deleted(self) -> bool:
        """Return whether the body is gone and this is a tombstone."""
        return bool(self.deleted_at)

    def to_json(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "id": self.id,
            "subject": self.subject,
            "author": self.author,
            "body": self.body,
            "question": self.question,
            "settles": self.settles,
            "supersedes": self.supersedes,
            "artifacts": [a.to_json() for a in self.artifacts],
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
            "deleted_by": self.deleted_by,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the wire form."""
        body = _require(obj, "body", str)
        if len(body) > MAX_BODY_CHARS:
            msg = f"note body is {len(body)} chars, limit is {MAX_BODY_CHARS}; reference an artifact instead"
            raise WireError(msg)
        settles = obj.get("settles")
        supersedes = obj.get("supersedes")
        return cls(
            id=int(obj.get("id") or 0),
            subject=normalize_subject(_require(obj, "subject", str)),
            author=_require(obj, "author", str),
            body=body,
            question=bool(obj.get("question")),
            settles=int(settles) if settles is not None else None,
            supersedes=int(supersedes) if supersedes is not None else None,
            artifacts=tuple(Artifact.from_json(a) for a in obj.get("artifacts") or ()),
            created_at=obj.get("created_at") or now(),
            deleted_at=str(obj.get("deleted_at") or ""),
            deleted_by=str(obj.get("deleted_by") or ""),
        )


@dataclass(frozen=True, slots=True)
class NoteEntry:
    """A note, what the hub computed about it, and what *this process* checked.

    Read carefully: the three fields have three different origins, and mixing
    them up is how a trust claim gets laundered.

    - `note` came off the wire.
    - `settled_by` is the hub's arithmetic over its own table — a fact about
      counts, not about who anybody is, so it is safe to parse for the same
      reason `Registration` is. `archived` is the same kind of fact, about the
      pile rather than the note: a subject read rolls up its children, so a
      finished pile's notes arrive inside a live parent's reading, and without
      this they arrive looking current.
    - `provenance` is **never parsed**. `from_json` ignores any `provenance` key
      the wire offers and leaves the honest default in place; only
      `checked()` may replace it, and only the code that ran a check calls that.
      A hub that sends `{"provenance": {"verified": true}}` changes nothing, and
      there is a test saying so.
    """

    note: Note
    settled_by: int | None = None
    superseded_by: int | None = None
    archived: bool = False
    provenance: Provenance = field(default_factory=Provenance.unverified)

    @property
    def is_open(self) -> bool:
        """Return whether this is a question nobody has answered yet."""
        return self.note.question and self.settled_by is None

    @property
    def is_current(self) -> bool:
        """Return whether this note is still what the subject says, as far as anyone has said otherwise."""
        return self.superseded_by is None and not self.note.deleted

    def checked(self, provenance: Provenance) -> Self:
        """Return a copy carrying the verdict of a check that actually ran."""
        return replace(self, provenance=provenance)

    def to_json(self) -> dict[str, Any]:
        """Return the rendering form.

        Output, not wire input: `cairn notes --json` prints it and no endpoint
        accepts it. The hub serializes `note` and `settled_by` by hand and never
        emits a provenance key, so this asymmetry is structural.
        """
        return {
            **self.note.to_json(),
            "settled_by": self.settled_by,
            "superseded_by": self.superseded_by,
            "archived": self.archived,
            "open": self.is_open,
            "current": self.is_current,
            "provenance": {
                "verified": self.provenance.verified,
                "method": self.provenance.method,
                "detail": self.provenance.detail,
            },
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse what the hub sends, deliberately dropping any asserted provenance."""
        settled_by = obj.get("settled_by")
        superseded_by = obj.get("superseded_by")
        return cls(
            note=Note.from_json(_require(obj, "note", dict)),
            settled_by=int(settled_by) if settled_by is not None else None,
            superseded_by=int(superseded_by) if superseded_by is not None else None,
            archived=bool(obj.get("archived")),
        )


@dataclass(frozen=True, slots=True)
class SubjectSummary:
    """One pile: what it is for, how much has collected on it, how much is unanswered.

    This is what `cairn notes` prints with no subject named, and it is the answer
    to the question the evidence in docs/design.md §12 item 4 was really asking:
    *is there anything here I should read before I start?*

    **`description` is what turned a subject from a string into a thing.** It used
    to be counts only, over a key that came into existence the moment somebody
    typed it, so the index could list `soak-441`, `eval-441` and `run-441` and give
    a reader no way to tell whether any of them was the pile they meant. A sentence
    per pile is the difference between an index you scan before writing and one you
    only understand after opening all three.

    `archived_at` is a string rather than a flag so that the reading can say when
    and the row can say who — a pile that vanished from the index with no date on
    it is the silent-loss shape this project keeps finding in other systems.

    **`described_at` and `described_by` exist because a description ages and
    nothing said so.** Every note is read beside a date and an author; the
    sentence a writer actually decides on had neither, so one written eighteen
    months ago and wrong for twelve read exactly as authoritative as one written
    this morning. `last_at` is not that date — it is the newest *note*, which
    reads as freshness for the pile and is silently not a claim about the
    description. An acceptance session put the consequence at *"six months out,
    negative — not zero"*, because a stale description does not fail quietly, it
    misroutes the next writer into a second pile. See `store.describe_subject`.
    """

    subject: str
    notes: int
    open_questions: int
    last_at: str
    description: str = ""
    created_by: str = ""
    archived_at: str = ""
    described_at: str = ""
    described_by: str = ""

    @property
    def archived(self) -> bool:
        """Return whether this pile is closed to new notes."""
        return bool(self.archived_at)

    def to_json(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "subject": self.subject,
            "notes": self.notes,
            "open_questions": self.open_questions,
            "last_at": self.last_at,
            "description": self.description,
            "created_by": self.created_by,
            "archived_at": self.archived_at,
            "described_at": self.described_at,
            "described_by": self.described_by,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the wire form, tolerating a hub that predates the subject table.

        The three new keys default to empty, which renders as a pile nobody has
        described — true of an older hub, where nobody could have. Unlike a
        window, nothing here is answered wrongly by their absence: the counts, the
        name and the date all still mean what they meant.

        `described_at` and `described_by` default the same way, and an older hub
        omitting them is read as "this build cannot tell you", which is the truth.
        A missing date here must never be shown as a fresh one — that would be the
        stale-description problem wearing the fix's clothes.
        """
        return cls(
            subject=normalize_subject(_require(obj, "subject", str)),
            notes=int(obj.get("notes") or 0),
            open_questions=int(obj.get("open_questions") or 0),
            last_at=str(obj.get("last_at") or ""),
            description=str(obj.get("description") or ""),
            created_by=str(obj.get("created_by") or ""),
            archived_at=str(obj.get("archived_at") or ""),
            described_at=str(obj.get("described_at") or ""),
            described_by=str(obj.get("described_by") or ""),
        )


Arrival = Literal["new", "returning", "takeover"]
"""Which of the three registration cases happened. See `store.register`."""


@dataclass(frozen=True, slots=True)
class Registration:
    """What registering a name did, beyond the record it left behind.

    Output only — the hub computes it, the client reports it, and no endpoint
    accepts it. It is not a trust claim, so unlike `Provenance` it is safe to
    parse: it says what the hub did, not who anybody is.

    It exists because a takeover moves the cursor, and that used to happen in
    silence. `resume_at` is the cursor as it stood before the jump, which makes
    the loss recoverable rather than merely reported — without it, the only way
    back is a hand-edited SQLite file.
    """

    agent: Agent
    arrival: Arrival = "new"
    skipped: int = 0
    previous: str = ""
    resume_at: int = 0

    def to_json(self) -> dict[str, Any]:
        """Return the rendering form, agent included."""
        return {
            "agent": self.agent.to_json(),
            "arrival": self.arrival,
            "skipped": self.skipped,
            "previous": self.previous,
            "resume_at": self.resume_at,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Self:
        """Parse the rendering form, tolerating a hub that predates these fields."""
        arrival = obj.get("arrival")
        return cls(
            agent=Agent.from_json(_require(obj, "agent", dict)),
            arrival=arrival if arrival in ("new", "returning", "takeover") else "new",
            skipped=int(obj.get("skipped") or 0),
            previous=str(obj.get("previous") or ""),
            resume_at=int(obj.get("resume_at") or 0),
        )


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload with the protocol version and the sender's clock.

    **`t` is here rather than on any one route because the alternative is a
    round trip.** Every relative time cairn prints — `peers` ages, and anything
    a reader computes from a `created_at` — is arithmetic against a clock, and
    until this existed that clock was always the *reader's*, subtracting a
    hub-stamped instant from a local `now`. Two machines is the premise of the
    whole tool, so those are two clocks, and the difference between them landed
    silently in every age. Riding the envelope, the hub's clock is on the
    response the caller already made, on every route, at no cost.

    A client's own POSTs carry it too, because this is one function and giving
    the two directions different envelopes to save four bytes would be a shape
    somebody has to remember. **The hub does not read it**, and must not: a
    timestamp from a peer is an assertion about that peer, and the hub stamps
    its own rows with `now()` for the same reason `Message` has no `verified`.

    Additive, so `PROTOCOL_VERSION` does not move: `check_version` reads `v` and
    every `from_json` here ignores keys it does not know — which `v` itself has
    always relied on.
    """
    return {"v": PROTOCOL_VERSION, "t": now(), **payload}


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
