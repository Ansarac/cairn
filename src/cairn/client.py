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

import contextlib
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

from cairn.errors import Unreachable, UsageError
from cairn.wire import (
    Agent,
    Artifact,
    InboxPage,
    Message,
    MessageKind,
    Note,
    NoteEntry,
    Registration,
    SubjectSummary,
    WireError,
    Withdrawal,
    dumps,
    envelope,
    loads,
)

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
        self.hub_time = ""
        """The hub's own clock, as of the last call this client made.

        Empty until a call has been made, and empty for good against a hub built
        before the envelope carried one — which is what `render._ago` treats as
        "fall back to this machine's clock", the behaviour that shipped for eight
        cuts. It is recorded here rather than returned because every route
        carries it and no caller wants a timestamp bolted onto the answer it
        actually asked for. See `wire.envelope`.
        """

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
                answer = loads(response.read())
                # Recorded on the way past, on every route, so that no command has
                # to spend a second round trip learning what time the hub thinks
                # it is. Only overwritten when the hub actually sent one: an old
                # hub must leave the previous answer alone rather than blanking it.
                self.hub_time = str(answer.get("t") or self.hub_time)
                return answer
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

    @staticmethod
    @contextlib.contextmanager
    def _readable() -> Iterator[None]:
        """Turn "the hub sent something this build cannot read" into exit 2.

        `_call` already does this for the envelope, but every caller then parses
        the payload *outside* that try — and `WireError` is a `ValueError`, so
        `run()` deliberately lets it through as a traceback plus exit 1, the code
        for "asked, nothing to report". That is the poisoned-mailbox shape from
        docs/design.md §12 item 3 wearing a different hat, and it applies to
        every list-comprehension parse in this file.

        Reaching it needs a hub that stores what its own reader rejects, which
        `store` is written to prevent on both tables. This is the second line:
        the parse is a real check — it is what stops a hostile hub sending a
        subject containing a newline and forging a column-zero header in
        `cairn notes` — so it has to keep raising, and it has to raise something
        the caller can act on. `KeyError` is included because a response missing
        the key it promised is the same failure through a smaller door.
        """
        try:
            yield
        except (WireError, KeyError) as exc:
            msg = f"hub spoke something unexpected: {exc}"
            raise Unreachable(msg) from exc

    def health(self) -> dict[str, Any]:
        """Return the hub's health payload."""
        return self._call("GET", "/v1/health")

    def register(self, agent: Agent) -> Registration:
        """Join the network, or refresh an existing registration.

        `Registration.from_json` fills defaults for a hub that predates the
        arrival fields, so a newer client against an older hub degrades to
        saying nothing rather than to a KeyError.
        """
        with self._readable():
            return Registration.from_json(self._call("POST", "/v1/register", agent.to_json()))

    def peers(self, exclude: str | None = None) -> list[Agent]:
        """List registered agents."""
        payload = self._call("GET", "/v1/peers", exclude=exclude)
        with self._readable():
            return [Agent.from_json(a) for a in payload["agents"]]

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
        answer = self._call("POST", "/v1/messages", payload)
        with self._readable():
            return Message.from_json(answer["message"])

    def inbox(self, agent: str, limit: int = 50, since: int = 0) -> InboxPage:
        """Fetch a page of unread mail for `agent`, oldest first, with the true totals.

        Returns the page even against a hub that predates the totals: they are
        two keys on an existing response, so their absence is not an outage and
        must not be reported as one. `InboxPage.from_json` derives both from the
        page in that case, which reproduces exactly what this client did before
        the totals existed — including the bell deafness they were added to fix.
        Degrading to the old behaviour is the honest answer; refusing to talk to
        the hub over it would not be.

        **A window gets the opposite treatment, and the asymmetry is the point.**
        A hub that does not understand `?since=` does not omit a number; it
        answers the unwindowed question in the windowed question's shape, and this
        client would hand the caller the oldest page of the whole backlog as
        though it were the part they had not read. Silently showing mail from the
        wrong end of a queue is worse than any error, so the hub's echo of the
        window it applied is checked, and a mismatch is a refusal — exit 3, "this
        cannot be carried out as asked", because the hub is up and answering and
        the only thing wrong is the flag.

        Checked on the echo rather than on the rows, deliberately. `any(m.seq <=
        since)` also detects it and does so only when the mailbox happens to hold
        a row old enough to give the game away, which makes an upgrade skew a bug
        that appears with the data and vanishes with it. This one is deterministic
        on the first call.
        """
        payload = self._call("GET", "/v1/inbox", agent=agent, limit=limit, since=since or None)
        with self._readable():
            page = InboxPage.from_json(payload)
        if since and page.since != since:
            msg = (
                f"this hub does not support --since (it ignored ?since={since}), so it would have shown you "
                f"the oldest page of the whole backlog instead of the part after seq {since}; "
                f"upgrade the hub, or read without the flag"
            )
            raise UsageError(msg)
        return page

    def sent(self, agent: str, limit: int = 50) -> tuple[list[Message], int]:
        """Fetch a page of what `agent` sent, oldest first, and the total it has sent.

        Reading this consumes nothing — there is no cursor on your own sends, so
        no ack follows and a second call returns the same rows.
        """
        payload = self._call("GET", "/v1/sent", agent=agent, limit=limit)
        with self._readable():
            return [Message.from_json(m) for m in payload["messages"]], int(payload.get("total") or 0)

    def ack(self, agent: str, seq: int, *, rewind: bool = False) -> int:
        """Move the agent's cursor and return where it now sits."""
        payload = {"agent": agent, "seq": seq, "rewind": rewind}
        answer = self._call("POST", "/v1/ack", payload)
        with self._readable():
            return int(answer["cursor"])

    def retract(self, seq: int, sender: str) -> Withdrawal:
        """Withhold a message from every mailbox that has not passed it yet."""
        answer = self._call("POST", "/v1/retract", {"seq": seq, "sender": sender})
        with self._readable():
            return Withdrawal.from_json(answer)

    def prune(self, older_than_days: int) -> tuple[int, int, tuple[str, ...]]:
        """Delete old messages nobody still has unread; return what went, what stayed, and whose.

        Days rather than an instant, deliberately. Every `created_at` on the hub
        was stamped by the hub's clock, so a cutoff computed here would be this
        machine's arithmetic about somebody else's timestamps — the two-clock bug
        docs/design.md §12 item 12 took out of `peers`, rebuilt on a command that
        deletes things.

        `kept_by` is empty against a hub that predates it, and `kept` stays the
        count of record for that reason: an empty list means "this hub did not
        say", never "nobody is holding anything".
        """
        answer = self._call("POST", "/v1/prune", {"older_than_days": older_than_days})
        with self._readable():
            return int(answer["removed"]), int(answer["kept"]), tuple(str(a) for a in answer.get("kept_by") or ())

    def write_note(  # noqa: PLR0913, PLR0917 - the note schema, same reasoning as `send`
        self,
        author: str,
        body: str,
        subject: str | None = None,
        question: bool = False,  # noqa: FBT001, FBT002 - mirrors the wire field
        settles: int | None = None,
        supersedes: int | None = None,
        artifacts: Sequence[Artifact] = (),
    ) -> Note:
        """Leave a note on a subject, or settle a question, and return what was stored.

        The returned note carries the subject the hub actually filed it under,
        which is not always the one passed in: subjects are case-folded, and a
        settling or superseding note inherits its target's. Callers print what
        came back rather than what they sent.

        **A hub that dropped `supersedes` is refused rather than believed**, on
        `inbox`'s reasoning and with a sharper consequence. An older hub does not
        reject the field, it ignores it — so the correction is stored, the command
        reports success, and the one thing that made it a correction is silently
        gone. What comes back is the row as stored, so the check is the row.
        """
        payload = {
            "author": author,
            "body": body,
            "subject": subject,
            "question": question,
            "settles": settles,
            "supersedes": supersedes,
            "artifacts": [a.to_json() for a in artifacts],
        }
        answer = self._call("POST", "/v1/notes", payload)
        with self._readable():
            stored = Note.from_json(answer["note"])
        if supersedes is not None and stored.supersedes != supersedes:
            msg = (
                f"this hub does not support supersede (it stored note {stored.id} without the link to "
                f"{supersedes}), so the correction would sit on the pile as an unrelated note; "
                f"upgrade the hub, or say in the body which note this replaces"
            )
            raise UsageError(msg)
        return stored

    def delete_note(self, note_id: int, author: str, reason: str) -> Note:
        """Take a note's body out, leaving a tombstone, and return what is left."""
        answer = self._call("POST", "/v1/notes/delete", {"id": note_id, "author": author, "reason": reason})
        with self._readable():
            return Note.from_json(answer["note"])

    def notes(
        self,
        subject: str | None = None,
        *,
        open_only: bool = False,
        find: str | None = None,
        limit: int = 50,
        deleted: bool = False,
    ) -> tuple[list[NoteEntry], int, int]:
        """Fetch a page of notes and the total matching the same filter.

        The total is what makes a truncated page distinguishable from a complete
        answer. Every caller is expected to compare the two and say so.
        """
        payload = self._call(
            "GET",
            "/v1/notes",
            subject=subject,
            open="1" if open_only else None,
            find=find,
            limit=limit,
            deleted="1" if deleted else None,
        )
        with self._readable():
            entries = [NoteEntry.from_json(n) for n in payload["notes"]]
        return entries, int(payload.get("total") or 0), int(payload.get("removed") or 0)

    def subjects(self, *, archived: bool = False) -> list[SubjectSummary]:
        """List every subject, with counts. Archived piles only when asked for."""
        payload = self._call("GET", "/v1/subjects", archived="1" if archived else None)
        with self._readable():
            return [SubjectSummary.from_json(s) for s in payload["subjects"]]

    def create_subject(self, name: str, description: str, author: str) -> SubjectSummary:
        """Open a pile deliberately, and return what the hub filed."""
        answer = self._call("POST", "/v1/subjects", {"name": name, "description": description, "author": author})
        with self._readable():
            return SubjectSummary.from_json(answer["subject"])

    def describe_subject(self, name: str, description: str, author: str) -> tuple[SubjectSummary, str]:
        """Correct a pile's description, returning its new state and the text it replaced.

        The replaced text is returned rather than dropped because the caller is
        the only one who can still show it to the person who overwrote it — see
        `store.describe_subject`. An older hub has no such route and `_call` turns
        that into `Unreachable`, which `cli._older_hub` reads as a hub age
        problem rather than a network one.
        """
        answer = self._call(
            "POST", "/v1/subjects/describe", {"name": name, "description": description, "author": author}
        )
        with self._readable():
            return SubjectSummary.from_json(answer["subject"]), str(answer.get("replaced") or "")

    def archive_subject(self, name: str, author: str, *, reopen: bool = False) -> SubjectSummary:
        """Close a pile to new notes, or open it again, and return its new state."""
        answer = self._call("POST", "/v1/subjects/archive", {"name": name, "author": author, "reopen": reopen})
        with self._readable():
            return SubjectSummary.from_json(answer["subject"])

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
