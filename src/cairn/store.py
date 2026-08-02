"""Durability, and the server-side cursor.

The store is the only thing in cairn that knows a message survives a reboot. It
does not know that HTTP exists, which is what lets the transport be replaced
without touching delivery semantics.

Two decisions here carry most of the weight.

**The cursor lives on the server.** A client never remembers where it got to.
An agent can be gone for a week, come back with an empty disk, and `unread()`
still returns exactly what it missed. This is the whole answer to "the peer was
switched off", and it costs one integer per agent.

**Notes are not messages with a longer shelf life.** They live in their own
table, have no recipient and no cursor, and reading one moves nothing. A message
is addressed to a session; a note is addressed to a *subject* and waits there for
whoever turns up next. Whether a question is still open is derived from whether
any note points at it, never stored — see `write_note`.

**A new agent starts at the end, a returning agent does not.** Registering a
name for the first time sets its cursor to the current head, so a fresh session
is not buried under a month of other people's mail. Re-registering the same
name — which is what a restarted session does — leaves the cursor alone, so the
backlog it missed is still waiting. Getting this backwards in either direction
is a bug users will feel immediately.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cairn.errors import UsageError
from cairn.wire import (
    BROADCAST,
    KINDS,
    MAX_BODY_CHARS,
    Agent,
    Arrival,
    Artifact,
    InboxPage,
    Message,
    MessageKind,
    Note,
    NoteEntry,
    Registration,
    SubjectSummary,
    normalize_subject,
    now,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name          TEXT PRIMARY KEY,
    machine       TEXT NOT NULL,
    cwd           TEXT NOT NULL,
    capabilities  TEXT NOT NULL,
    session_id    TEXT,
    registered_at TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    sender         TEXT NOT NULL,
    recipient      TEXT NOT NULL,
    body           TEXT NOT NULL,
    correlation_id TEXT,
    artifacts      TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_recipient ON messages (recipient, seq);
CREATE INDEX IF NOT EXISTS messages_by_sender ON messages (sender, seq);
CREATE TABLE IF NOT EXISTS cursors (
    agent          TEXT PRIMARY KEY,
    last_acked_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    question   INTEGER NOT NULL DEFAULT 0,
    settles    INTEGER REFERENCES notes (id),
    supersedes INTEGER REFERENCES notes (id),
    artifacts  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    deleted_by TEXT
);
CREATE INDEX IF NOT EXISTS notes_by_subject ON notes (subject, id);
CREATE INDEX IF NOT EXISTS notes_by_settles ON notes (settles);
CREATE INDEX IF NOT EXISTS notes_by_supersedes ON notes (supersedes);
CREATE TABLE IF NOT EXISTS subjects (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    archived_at TEXT,
    archived_by TEXT
);
"""

_BACKFILL = """
INSERT OR IGNORE INTO subjects (name, description, created_by, created_at)
SELECT n.subject, ?, ?, MIN(n.created_at) FROM notes n GROUP BY n.subject
"""
"""Give every subject that already has notes a row, once, at open.

A subject used to be a string in the `notes` table and nothing else, so there is
no upgrade step that could have created these rows and no operator who knows they
are needed — including on the hub that is running in a container right now, with
real sediment on it. Making the schema self-repairing is the only version of this
that does not require somebody to be told something.

`MIN(created_at)` rather than `now()`, because the pile genuinely started when its
first note landed and dating it to the upgrade would make every existing subject
look new on the one reading where age is the point. The description says outright
that nobody wrote one; inventing a plausible sentence here would be worse than
admitting the gap, and it is the one nudge that gets these subjects described.
"""

_MIGRATIONS = (
    "ALTER TABLE notes ADD COLUMN supersedes INTEGER REFERENCES notes (id)",
    "ALTER TABLE notes ADD COLUMN deleted_at TEXT",
    "ALTER TABLE notes ADD COLUMN deleted_by TEXT",
)
"""Columns added to a table that already exists, on the same terms as `_BACKFILL`.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that is already there, so a
schema change to `notes` reaches an existing database only through `ALTER TABLE`.
Each of these runs at open and each is expected to fail once the column is there;
the duplicate-column error is the success case on every run after the first. That
is uglier than a version table and it is the shape that cannot get out of step
with the hub in the container, which nobody is going to run a migration against.
"""

BACKFILLED_DESCRIPTION = "(no description — this subject predates `cairn subject`)"
BACKFILLED_AUTHOR = "unknown"


class Store(Protocol):
    """What the hub needs from durability, and nothing more."""

    def register(self, agent: Agent) -> Registration:
        """Add or refresh an agent, and report which of the three cases it was."""
        ...

    def get_agent(self, name: str) -> Agent | None:
        """Return one agent, or None."""
        ...

    def peers(self, exclude: str | None = None) -> list[Agent]:
        """Return every registered agent, optionally omitting one."""
        ...

    def append(  # noqa: PLR0913, PLR0917 - these six are the message schema; collapsing them would hide the contract
        self,
        kind: MessageKind,
        sender: str,
        recipient: str,
        body: str,
        correlation_id: str | None = None,
        artifacts: Sequence[Artifact] = (),
    ) -> Message:
        """Durably record a message and return it with its assigned sequence."""
        ...

    def unread(self, agent: str, limit: int = 50, since: int = 0) -> InboxPage:
        """Return a page of messages after the agent's cursor, plus the true count and head."""
        ...

    def sent(self, agent: str, limit: int = 50) -> tuple[list[Message], int]:
        """Return a page of what the agent sent, oldest first, and the total."""
        ...

    def ack(self, agent: str, seq: int, *, rewind: bool = False) -> int:
        """Move the cursor to `seq` and return where it now sits."""
        ...

    def write_note(  # noqa: PLR0913, PLR0917 - the note schema, same reasoning as `append`
        self,
        author: str,
        body: str,
        subject: str | None = None,
        question: bool = False,  # noqa: FBT001, FBT002 - mirrors `Note.question`; keyword-only here would not match the wire
        settles: int | None = None,
        supersedes: int | None = None,
        artifacts: Sequence[Artifact] = (),
    ) -> Note:
        """Durably record a note and return it with its assigned id."""
        ...

    def get_note(self, note_id: int) -> Note | None:
        """Return one note, or None."""
        ...

    def delete_note(self, note_id: int, author: str, reason: str) -> Note:
        """Take a note's body out, leaving a tombstone that says who and why."""
        ...

    def notes(
        self,
        subject: str | None = None,
        *,
        open_only: bool = False,
        find: str | None = None,
        limit: int = 50,
        deleted: bool = False,
    ) -> tuple[list[NoteEntry], int, int]:
        """Return a page of notes, the total the filter matched, and how many are tombstones."""
        ...

    def subjects(self, *, archived: bool = False) -> list[SubjectSummary]:
        """Return every subject, with counts. Archived ones only when asked for."""
        ...

    def create_subject(self, name: str, description: str, author: str) -> SubjectSummary:
        """Open a new pile deliberately, and return it."""
        ...

    def archive_subject(self, name: str, author: str, *, reopen: bool = False) -> SubjectSummary:
        """Close a subject to new notes, or open it again. Reading is unaffected."""
        ...


class SqliteStore:
    """A `Store` backed by one SQLite file.

    Pass `:memory:` for tests. There is deliberately no separate in-memory
    implementation: two implementations means the one under test is not the one
    in production.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        """Open `path`, creating the schema if it is not there yet."""
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        for statement in _MIGRATIONS:
            with contextlib.suppress(sqlite3.OperationalError):
                self._db.execute(statement)
        self._db.execute(_BACKFILL, (BACKFILLED_DESCRIPTION, BACKFILLED_AUTHOR))

    def close(self) -> None:
        """Close the underlying connection."""
        self._db.close()

    # -- agents ---------------------------------------------------------------

    def register(self, agent: Agent) -> Registration:
        """Upsert the row, and decide whether the cursor survives.

        Three cases, and the distinction between the last two is the whole point.

        A **new name** parks at the head, so a fresh session is not buried under
        a month of other people's mail.

        A **returning session** — same name, same `(machine, cwd)` — keeps its
        cursor, so a restart still receives the backlog it actually missed.

        A **takeover** — the same name arriving from somewhere else — parks at
        the head too. Nothing separated these two before, and the consequence was
        reproduced against a live hub: a second session registered an existing
        name from another directory on another machine, inherited the cursor, and
        read mail addressed to its predecessor. Neither was told.

        `(machine, cwd)` is the discriminator because it is already carried, is
        always populated, and is exactly the pair that "restarted in the same
        directory" holds fixed. `session_id` would be stronger evidence and is
        still stored, but a product that exports none leaves it `None`, so it
        cannot be the test.

        This does not stop a takeover — I3, and the hub cannot know which
        claimant is legitimate. It stops the newcomer from silently inheriting a
        conversation. The sending side refuses separately; see `config.check_pin`.
        """
        existing = self.get_agent(agent.name)
        moved = existing is not None and (existing.machine, existing.cwd) != (agent.machine, agent.cwd)
        # Captured before the cursor moves, so the report can say what was skipped
        # and where to resume from. Without `resume_at` the loss is unrecoverable:
        # `ack` will not rewind, so the only way back would be editing the database.
        resume_at = self._cursor(agent.name) if moved else 0
        # `limit=0` because the page is not wanted, only the count behind it —
        # which `unread` now returns uncapped. This is what retires a constant
        # whose entire job was to make a list long enough to be counted.
        skipped = self.unread(agent.name, limit=0).unread if moved else 0
        stamped = Agent(
            name=agent.name,
            machine=agent.machine,
            cwd=agent.cwd,
            capabilities=agent.capabilities,
            session_id=agent.session_id,
            # A takeover is a new arrival, so it gets a new registration date.
            # Keeping the old one would date the newcomer to its predecessor.
            registered_at=existing.registered_at if existing and not moved else agent.registered_at,
            last_seen=now(),
        )
        self._db.execute(
            """INSERT INTO agents (name, machine, cwd, capabilities, session_id, registered_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   machine=excluded.machine, cwd=excluded.cwd, capabilities=excluded.capabilities,
                   session_id=excluded.session_id, last_seen=excluded.last_seen""",
            (
                stamped.name,
                stamped.machine,
                stamped.cwd,
                json.dumps(list(stamped.capabilities)),
                stamped.session_id,
                stamped.registered_at,
                stamped.last_seen,
            ),
        )
        if existing is None:
            # A name seen for the first time starts at the head, not at zero.
            # A name coming back — a restarted session — keeps whatever it had.
            self._db.execute(
                "INSERT OR IGNORE INTO cursors (agent, last_acked_seq) VALUES (?, ?)",
                (stamped.name, self._head()),
            )
        elif moved:
            # Someone else now holds this name. Move the cursor forward to the
            # head so the newcomer starts clean; `ack` refuses to rewind, so this
            # is the one place a cursor may jump, and it only ever jumps forward.
            self._db.execute(
                """INSERT INTO cursors (agent, last_acked_seq) VALUES (?, ?)
                   ON CONFLICT(agent) DO UPDATE SET last_acked_seq = MAX(last_acked_seq, excluded.last_acked_seq)""",
                (stamped.name, self._head()),
            )
        arrival: Arrival = "takeover" if moved else ("returning" if existing else "new")
        previous = f"{existing.machine}:{existing.cwd}" if moved and existing else ""
        return Registration(agent=stamped, arrival=arrival, skipped=skipped, previous=previous, resume_at=resume_at)

    def get_agent(self, name: str) -> Agent | None:
        """Look one agent up by its exact registered name."""
        row = self._db.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        return _agent_from_row(row) if row else None

    def peers(self, exclude: str | None = None) -> list[Agent]:
        """Return every agent, name-ordered, dropping `exclude` if it is registered."""
        rows = self._db.execute("SELECT * FROM agents ORDER BY name").fetchall()
        return [_agent_from_row(r) for r in rows if r["name"] != exclude]

    # -- messages -------------------------------------------------------------

    def append(  # noqa: PLR0913, PLR0917 - these six are the message schema; collapsing them would hide the contract
        self,
        kind: MessageKind,
        sender: str,
        recipient: str,
        body: str,
        correlation_id: str | None = None,
        artifacts: Sequence[Artifact] = (),
    ) -> Message:
        """Insert the message, refusing an unknown kind, sender, or misaddressed recipient.

        The kind check is the newest of the three and it is a bug fix, not
        symmetry. `hub._send` passes `obj.get("kind", "tell")` straight in here,
        and `Message.from_json` rejects what this used to accept — so one POST
        of `{"kind": "shout"}` was stored durably and answered 200, and from
        then on every `cairn inbox` for that recipient raised `WireError` out of
        the list comprehension in `client.inbox`. That is a `ValueError`, not a
        `CairnError`, so `run()` does not catch it: the reader got a traceback
        and exit 1 — the code for "asked, nothing to report". A poisoned mailbox
        therefore read as "no mail" to every script that follows the skill,
        forever, with no seq printed to aim an `ack` past. Reproduced against a
        live hub.
        """
        if kind not in KINDS:
            msg = f"unknown message kind {kind!r}; this hub stores only: {', '.join(KINDS)}"
            raise UsageError(msg)
        if self.get_agent(sender) is None:
            msg = f"unknown sender {sender!r}; register before sending"
            raise UsageError(msg)
        if recipient != BROADCAST and self.get_agent(recipient) is None:
            known = ", ".join(a.name for a in self.peers()) or "nobody yet"
            msg = f"unknown recipient {recipient!r}; registered agents are: {known}"
            raise UsageError(msg)
        self._touch(sender)
        created = now()
        cursor = self._db.execute(
            """INSERT INTO messages (kind, sender, recipient, body, correlation_id, artifacts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (kind, sender, recipient, body, correlation_id, json.dumps([a.to_json() for a in artifacts]), created),
        )
        return Message(
            seq=int(cursor.lastrowid or 0),
            kind=kind,
            sender=sender,
            recipient=recipient,
            body=body,
            correlation_id=correlation_id,
            artifacts=tuple(artifacts),
            created_at=created,
        )

    def unread(self, agent: str, limit: int = 50, since: int = 0) -> InboxPage:
        """Select what is addressed to `agent` past its cursor, never its own sends.

        The page is the **oldest** `limit` rows, and that is the opposite of
        `sent` and `notes` on purpose: a queue is read from the front, and a
        reader working through a backlog must not have the front of it silently
        dropped. It is also why a `--wait` may only ever run on an *empty* window
        — a poll loop on a truncated one would never reach the answer.

        `unread` and `head` are computed over the same predicate **without the
        limit**, and that is this method's job rather than a convenience. Both
        used to be inferred from the page by every caller, and both inferences
        are wrong the moment the backlog passes the cap. The count understated it
        in silence; the head simply stopped moving, which pinned `cairn bell`'s
        latch and took the turn-boundary bell permanently off the air. See
        `wire.InboxPage`.

        Three statements rather than one window function: this file is read by
        people reasoning about delivery, and `COUNT`/`MAX` over a named predicate
        says what it does at a glance.

        **`since` moves the floor of the page forward and moves nothing else.**
        It is the offset a live session went looking for and did not find: with
        `--limit` as the only control, the way to see past the first page is to
        raise the limit, which re-fetches everything already read — so a session
        with sixty-three waiting fetched the same fifty rows three times and then
        cut a record in half with `tail -c`. The floor applied is
        `max(cursor, since)`, so this can only ever narrow what the cursor already
        allows: every row it returns is unread mail the caller would have received
        anyway, which is what keeps a windowed read from mixing consumed and
        unconsumed traffic on one page with nothing marking which is which.

        **The window is deliberately not applied to `unread` and `head`.** Those
        two are facts about the mailbox and the bell reads them; recomputing them
        under a caller's window would make "unread" mean whatever the last reader
        typed. `matching` is the windowed count, it is what a truncation line has
        to be measured against, and it equals `unread` whenever no window is in
        force. See `wire.InboxPage`.

        The `_touch` is why a blocked reader looks busy. `cairn inbox --wait`
        polls through here — about five times over a quiet 60-second wait — and
        each poll refreshes `last_seen`, so a session doing nothing but standing
        still shows in `peers` as the freshest agent on the hub. True, and
        misleading in the one direction a peer asking "is the bench still there"
        cares about. Documented rather than fixed: a `touch: bool` parameter was
        measured at 0.093 ms/call against 0.014 ms read-only, and skipping the
        write would cost the liveness signal for the ordinary read too.
        """
        self._touch(agent)
        floor = max(0, since)
        # The cursor stays a subselect rather than the integer read below it, and
        # the difference is not style: `seq > NULL` is NULL, so a name with no
        # cursor row — an agent that never registered — selects nothing. Binding
        # `_cursor`'s zero instead would hand every unregistered name every
        # broadcast on the hub.
        where = """FROM messages
               WHERE seq > (SELECT last_acked_seq FROM cursors WHERE agent = ?)
                 AND (recipient = ? OR recipient = ?)
                 AND sender != ?"""
        scope: tuple[object, ...] = (agent, agent, BROADCAST, agent)
        totals = self._db.execute(f"SELECT COUNT(*) AS c, COALESCE(MAX(seq), 0) AS h {where}", scope).fetchone()
        matching = int(totals["c"])
        if floor:
            # The clause is a literal and the floor is a bound parameter, as
            # everywhere else in this file. A fourth statement rather than a
            # conditional inside the third: the extra count is what a windowed
            # read costs, and it should be visible that it is only paid then.
            where, scope = f"{where} AND seq > ?", (*scope, floor)
            matching = int(self._db.execute(f"SELECT COUNT(*) AS c {where}", scope).fetchone()["c"])
        rows = self._db.execute(f"SELECT * {where} ORDER BY seq LIMIT ?", (*scope, limit)).fetchall()
        return InboxPage(
            messages=tuple(_message_from_row(r) for r in rows),
            unread=int(totals["c"]),
            head=int(totals["h"]),
            floor=max(self._cursor(agent), floor),
            since=floor,
            matching=matching,
        )

    def sent(self, agent: str, limit: int = 50) -> tuple[list[Message], int]:
        """Select what `agent` sent, newest page handed back oldest-first, with the total.

        **No cursor is read and none is written**, and that absence is the whole
        shape of this table's second reader. A cursor answers "what have I not
        seen yet"; you have seen your own sends by definition, so there is
        nothing here to consume and nothing a second read can miss. It is the
        same absence `notes` has, arrived at from the opposite direction — a note
        has no recipient, a send has no *unread* recipient in you.

        The page is the **newest** `limit` rows, reversed to oldest-first. Same
        contract and same reasoning as `notes`: truncation drops the oldest
        traffic rather than this shift's, the reading order stays chronological,
        and the total ships alongside so a caller can tell a full page from a
        complete answer. `unread` is the counter-example and a known defect —
        it takes the *oldest* N and says nothing, which is how the turn-boundary
        bell goes deaf past its limit (see the appendix of docs/design.md).

        `_touch`, like `unread` and `ack`: a named agent ran a command, so it was
        heard from. Unlike `unread` this is not on a poll path, so it cannot
        inflate a blocked waiter's freshness.
        """
        self._touch(agent)
        total = int(self._db.execute("SELECT COUNT(*) AS c FROM messages WHERE sender = ?", (agent,)).fetchone()["c"])
        rows = self._db.execute(
            "SELECT * FROM messages WHERE sender = ? ORDER BY seq DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
        return [_message_from_row(r) for r in reversed(rows)], total

    def ack(self, agent: str, seq: int, *, rewind: bool = False) -> int:
        """Move the cursor forward, or backward when asked explicitly.

        Forward-only is the default and the reason is ordering, not policy: acks
        arrive out of order, and one for old mail must not undo a newer one.

        `rewind` is a different intent, and it needs its own door. A takeover
        jumps the cursor to the head, so a session that merely moved directory
        loses a backlog that is still sitting in `messages` — reachable, but only
        if something is allowed to move the cursor back. Without this the sole
        remedy is editing the database by hand, which is not a remedy.
        """
        self._touch(agent)
        # Two whole statements rather than one with an interpolated clause: the
        # difference between them is the entire point of the flag, and splicing
        # SQL to express it would hide that behind a suppressed warning.
        if rewind:
            self._db.execute(
                """INSERT INTO cursors (agent, last_acked_seq) VALUES (?, ?)
                   ON CONFLICT(agent) DO UPDATE SET last_acked_seq = excluded.last_acked_seq""",
                (agent, seq),
            )
        else:
            self._db.execute(
                """INSERT INTO cursors (agent, last_acked_seq) VALUES (?, ?)
                   ON CONFLICT(agent) DO UPDATE SET
                       last_acked_seq = MAX(last_acked_seq, excluded.last_acked_seq)""",
                (agent, seq),
            )
        row = self._db.execute("SELECT last_acked_seq FROM cursors WHERE agent = ?", (agent,)).fetchone()
        return int(row["last_acked_seq"])

    # -- notes ----------------------------------------------------------------

    def write_note(  # noqa: PLR0913, PLR0917 - the note schema, same reasoning as `append`
        self,
        author: str,
        body: str,
        subject: str | None = None,
        question: bool = False,  # noqa: FBT001, FBT002 - mirrors `Note.question`; keyword-only here would not match the wire
        settles: int | None = None,
        supersedes: int | None = None,
        artifacts: Sequence[Artifact] = (),
    ) -> Note:
        """Insert a note, deriving the subject when this one settles or supersedes another.

        Three refusals, each closing a way for sediment to become useless.

        **An unregistered author.** Same rule as `append`: a name that nobody
        can look up is not attribution.

        **An empty body.** A note with nothing in it still occupies a subject and
        still shows in the counts, so it costs a future reader a read and tells
        them nothing.

        **Settling something that is not an open question.** `--settles` exists
        to close a loop; pointing it at a statement would make `open` mean
        whatever the last caller felt like.

        A settling note **inherits its target's subject** rather than being given
        one. That removes an entire class of mistake — an answer filed under a
        different subject from its question is an answer nobody finds — and it is
        why `cairn settle` takes an id and no subject.

        A settling note is never itself a question. An answer that raises a new
        question is a second note on the same subject; folding both into one row
        would make "is this open" ambiguous for the one field whose whole value
        is that it is not.

        **`supersedes` is the same machinery pointed at statements**, and it
        inherits the subject for the same reason: a correction filed away from the
        thing it corrects is a correction nobody finds. It refuses to point at a
        question, because a question is not a claim that can be replaced — that is
        what `settles` is for, and allowing both would give `open` two meanings.
        Unlike `settles` it may point at something already superseded: corrections
        get corrected, and `notes` resolves a chain to its most recent end.
        """
        if self.get_agent(author) is None:
            msg = f"unknown author {author!r}; register before writing a note"
            raise UsageError(msg)
        subject, question = self._related(subject, question=question, settles=settles, supersedes=supersedes)
        if subject is None:
            msg = "a note needs a subject: the rig, run or board it is about"
            raise UsageError(msg)
        subject = normalize_subject(subject)
        # The pile has to exist, and this refusal is where subject sprawl is
        # actually stopped — `_no_such_subject` carries the argument. A settling
        # note reaches here with its target's subject, which exists by
        # construction, so it pays the lookup and never the refusal.
        pile = self._require_subject(subject)
        if pile.archived:
            msg = (
                f"subject {subject!r} was archived on {pile.archived_at} and takes no new notes.\n"
                f"  reading it still works: cairn notes {subject}\n"
                f"  if this pile is live again, say so first: cairn subject {subject} --reopen"
            )
            raise UsageError(msg)
        text = body.strip()
        if not text:
            msg = "a note with no body is not sediment; say what a reader six months from now needs to know"
            raise UsageError(msg)
        if len(text) > MAX_BODY_CHARS:
            msg = f"note body is {len(text)} chars, limit is {MAX_BODY_CHARS}; reference an artifact instead"
            raise UsageError(msg)
        self._touch(author)
        created = now()
        cursor = self._db.execute(
            """INSERT INTO notes (subject, author, body, question, settles, supersedes, artifacts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                subject,
                author,
                text,
                int(question),
                settles,
                supersedes,
                json.dumps([a.to_json() for a in artifacts]),
                created,
            ),
        )
        return Note(
            id=int(cursor.lastrowid or 0),
            subject=subject,
            author=author,
            body=text,
            question=question,
            settles=settles,
            supersedes=supersedes,
            artifacts=tuple(artifacts),
            created_at=created,
        )

    def _related(
        self,
        subject: str | None,
        *,
        question: bool,
        settles: int | None,
        supersedes: int | None,
    ) -> tuple[str | None, bool]:
        """Resolve the subject and questionhood a note inherits from the note it points at.

        Both relations take their subject from their target rather than from an
        argument, and that is what removes an entire class of mistake: an answer
        filed away from its question, or a correction filed away from the claim it
        corrects, is one nobody finds. It is also why neither `cairn settle` nor
        `cairn supersede` takes a subject.

        The two refusals point at each other on purpose. Somebody who reaches for
        the wrong verb has correctly identified that a note needs replacing or
        answering and picked the other one, so the message names the right command
        with the id already in it.
        """
        if settles is not None and supersedes is not None:
            msg = "a note either settles a question or supersedes a statement, not both"
            raise UsageError(msg)
        if settles is not None:
            target = self._target(settles, "settle")
            if not target.question:
                msg = (
                    f"note {settles} is not a question, so there is nothing to settle; "
                    f'to replace what it says: cairn supersede {settles} "<what is true now>"'
                )
                raise UsageError(msg)
            return target.subject, False
        if supersedes is not None:
            replaced = self._target(supersedes, "supersede")
            if replaced.question:
                msg = (
                    f"note {supersedes} is a question, and a question is not a claim to be replaced; "
                    f'to answer it: cairn settle {supersedes} "<what you found>"'
                )
                raise UsageError(msg)
            return replaced.subject, question
        return subject, question

    def _target(self, note_id: int, verb: str) -> Note:
        found = self.get_note(note_id)
        if found is None:
            msg = f"no note {note_id} to {verb}"
            raise UsageError(msg)
        if found.deleted:
            msg = f"note {note_id} was deleted on {found.deleted_at}; there is nothing left to {verb}"
            raise UsageError(msg)
        return found

    def get_note(self, note_id: int) -> Note | None:
        """Look one note up by id."""
        row = self._db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _note_from_row(row) if row else None

    def delete_note(self, note_id: int, author: str, reason: str) -> Note:
        """Take a note's body out and leave a tombstone in its place.

        **The body genuinely goes, and that is the point rather than a side
        effect.** Tidying a noisy pile could be done by hiding; the other reason
        anyone reaches for this is that the body should never have been written
        down — a credential, an internal hostname, a path under somebody's home
        directory — and a note that is merely hidden is still sitting in the
        database being handed to whoever runs a search. So the text is replaced.

        **The row survives, keeping its id.** Anything pointing at this note — a
        settling answer, a superseding correction — still resolves rather than
        dangling, and the pile can still say that something was here and who took
        it out. Deleting the row instead would make cairn the thing docs/design.md
        §10 criticises other systems for: a loss with nothing to show for it.

        The reason replaces the body rather than living in a column of its own,
        because it is what a reader of this pile now needs to see in the place
        they would have read the note. Say why it went, not what it said.

        No ownership check, on the same reasoning as `settle` and
        `archive_subject`: cairn does not authenticate, so a check would be a
        pretence — I3 — and the tombstone names who did it, which is the
        accountability that actually exists.
        """
        if self.get_agent(author) is None:
            msg = f"unknown author {author!r}; register before deleting a note"
            raise UsageError(msg)
        note = self.get_note(note_id)
        if note is None:
            msg = f"no note {note_id} to delete"
            raise UsageError(msg)
        if note.deleted:
            msg = f"note {note_id} was already deleted on {note.deleted_at} by {note.deleted_by}"
            raise UsageError(msg)
        text = " ".join(reason.split()).strip()
        if not text:
            msg = (
                f'deleting note {note_id} needs a reason: cairn delete {note_id} "<why it went>".\n'
                f"  it replaces the body, so it is what the next reader sees where the note was"
            )
            raise UsageError(msg)
        self._touch(author)
        when = now()
        self._db.execute(
            "UPDATE notes SET body = ?, deleted_at = ?, deleted_by = ? WHERE id = ?",
            (text, when, author, note_id),
        )
        return _note_from_row(self._db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())

    def notes(
        self,
        subject: str | None = None,
        *,
        open_only: bool = False,
        find: str | None = None,
        limit: int = 50,
        deleted: bool = False,
    ) -> tuple[list[NoteEntry], int, int]:
        """Return a page of notes, the total the filter matched, and how many are tombstones.

        The total is returned rather than inferred, and that is the fix for a
        defect this project already has elsewhere: `cairn inbox` truncates at
        `--limit` and says nothing, which is how the turn-boundary bell goes
        permanently deaf past that limit — see the appendix of docs/design.md. A
        caller that cannot tell a full page from a complete answer will
        eventually treat one as the other, so the count ships with the page.

        The page is the **newest** matches, handed back oldest-first. Truncation
        therefore drops ancient sediment rather than today's, while the reading
        order stays chronological — a pile is read forwards even when only its
        top is shown.

        `settled_by` is the *first* note that settled each question. A second
        opinion is allowed to arrive later and is stored like anything else; what
        it does not do is reopen the question or replace the answer of record.

        A subject read includes everything filed **under** it — see the prefix
        clause below. The index does not roll up, deliberately: it lists the
        piles that exist, while a read answers "what is known about this thing",
        and those are different questions.

        **Tombstones are out of the page and into a count**, because a pile that
        has been tidied should read as tidy while still saying that tidying
        happened. A reading that silently omitted them would be the quiet loss
        this project keeps finding elsewhere; one full of them would defeat the
        reason anybody deleted anything. The count is over the same filter, and
        `deleted=True` lists them.

        **A deleted note stops settling and stops superseding.** Both derived
        pointers ignore tombstones, so removing a wrong answer reopens its
        question and removing a wrong correction restores what it replaced. The
        alternative — a question closed forever by a note that no longer says
        anything — is the shape `open` exists to prevent.

        `superseded_by` is the **latest** note pointing here, and that is the
        deliberate opposite of `settled_by`, which is the first. An answer of
        record is the first one and a later opinion does not displace it; a
        correction of record is the most recent, because that is what a chain of
        corrections means.
        """
        where = ["n.deleted_at IS NOT NULL" if deleted else "n.deleted_at IS NULL"]
        params: list[object] = []
        if subject is not None:
            # Reading a subject includes everything under it. `/` is a legal
            # subject character, so `rig-a/chamber` is a natural thing to write —
            # and a reader who writes it has been invited by the character to
            # believe `cairn notes rig-a` will find it. Without this clause it
            # does not, and the note is invisible from the only place anyone
            # would look for it. Found by a session writing the docs, which had
            # read the character set and drawn exactly that conclusion.
            #
            # `_like_escape` is not optional here: `_` is in the permitted set,
            # so an unescaped prefix of `rig_a` would also match `rigXa/…`.
            root = normalize_subject(subject)
            where.append("(n.subject = ? OR n.subject LIKE ? ESCAPE '\\')")
            params += [root, f"{_like_escape(root)}/%"]
        if open_only:
            where.append(
                "n.question = 1 AND NOT EXISTS (SELECT 1 FROM notes s WHERE s.settles = n.id AND s.deleted_at IS NULL)"
            )
        if find:
            where.append("(n.body LIKE ? ESCAPE '\\' OR n.subject LIKE ? ESCAPE '\\')")
            pattern = f"%{_like_escape(find)}%"
            params += [pattern, pattern]
        clause = " AND ".join(where)
        total = int(self._db.execute(f"SELECT COUNT(*) AS c FROM notes n WHERE {clause}", params).fetchone()["c"])  # noqa: S608 - `clause` is built from literals above; every value is a bound parameter
        # The tombstone count is the same filter with the liveness clause flipped,
        # so it can never disagree with the page about what was being looked at.
        buried = clause.replace(where[0], "n.deleted_at IS NOT NULL" if not deleted else "n.deleted_at IS NULL", 1)
        removed = int(self._db.execute(f"SELECT COUNT(*) AS c FROM notes n WHERE {buried}", params).fetchone()["c"])  # noqa: S608 - ditto
        rows = self._db.execute(
            f"""SELECT n.*,
                       (SELECT MIN(s.id) FROM notes s WHERE s.settles = n.id AND s.deleted_at IS NULL) AS settled_by,
                       (SELECT MAX(s.id) FROM notes s WHERE s.supersedes = n.id AND s.deleted_at IS NULL)
                           AS superseded_by
                FROM notes n WHERE {clause} ORDER BY n.id DESC LIMIT ?""",  # noqa: S608 - ditto
            [*params, limit],
        ).fetchall()
        entries = [
            NoteEntry(
                note=_note_from_row(r),
                settled_by=int(r["settled_by"]) if r["settled_by"] is not None else None,
                superseded_by=int(r["superseded_by"]) if r["superseded_by"] is not None else None,
            )
            for r in reversed(rows)
        ]
        return entries, total, removed

    def subjects(self, *, archived: bool = False) -> list[SubjectSummary]:
        """Return every subject, most in need of attention first.

        Ordered by open questions and then by recency, because the question this
        answers is "is there anything here I should read before I start" — and an
        alphabetical list makes the reader do that sort themselves, every time.

        **A `LEFT JOIN` from `subjects`, not a `GROUP BY` over `notes`**, and that
        inversion is the whole of what changed when a subject stopped being a
        string. A pile with no notes on it yet is now a real thing — somebody
        opened it and said what it is for, which is exactly the state a reader
        needs to see *before* inventing a fifth spelling of the same run. Grouping
        over notes cannot represent it: it can only report piles that already have
        sediment, so the one moment the index could prevent a duplicate is the one
        moment it had nothing to show.

        Archived subjects are out unless asked for. Archiving says a pile is
        finished, and an index that keeps showing finished work is the sprawl this
        change exists to stop, arriving a second way.
        """
        rows = self._db.execute(
            f"""SELECT s.name AS subject, s.description AS description, s.created_by AS created_by,
                       s.created_at AS created_at, s.archived_at AS archived_at,
                       COUNT(CASE WHEN n.deleted_at IS NULL THEN n.id END) AS notes,
                       COALESCE(SUM(CASE WHEN n.question = 1 AND n.deleted_at IS NULL
                                          AND NOT EXISTS (SELECT 1 FROM notes x
                                                          WHERE x.settles = n.id AND x.deleted_at IS NULL)
                                         THEN 1 ELSE 0 END), 0) AS open_questions,
                       COALESCE(MAX(n.created_at), s.created_at) AS last_at
                FROM subjects s LEFT JOIN notes n ON n.subject = s.name
                {"" if archived else "WHERE s.archived_at IS NULL"}
                GROUP BY s.name
                ORDER BY open_questions DESC, last_at DESC"""  # noqa: S608 - the only interpolation is one of two literal clauses
        ).fetchall()
        return [_summary_from_row(r) for r in rows]

    def create_subject(self, name: str, description: str, author: str) -> SubjectSummary:
        """Open a pile deliberately, refusing a name that is already open.

        **The description is required, and it is the point of the command.** A
        subject used to be created as a side effect of writing the first note to
        it, so `soak-441`, `eval-441`, `run-441` and `441` were four piles a hub
        would happily create and creating one looked exactly like adding to one.
        Measured: an acceptance session invented `run-442` beside existing notes
        that talked about run 441, and said so itself afterwards — *"someone
        searching run-441 won't roll up into it."* What stops the fifth spelling is
        not the extra keystroke; it is that the index now says what each pile is
        for, so the next writer can tell whether theirs already exists.

        Re-creating an existing name is refused rather than treated as an update.
        Two writers describing one pile differently is the same divergence in a
        smaller font, and the second one is usually somebody who did not know the
        first existed — which is precisely the reader this is trying to catch.
        """
        if self.get_agent(author) is None:
            msg = f"unknown author {author!r}; register before opening a subject"
            raise UsageError(msg)
        subject = normalize_subject(name)
        text = " ".join(description.split()).strip()
        if not text:
            msg = (
                f"subject {subject!r} needs a description: one line saying what it is, so the next person "
                f"can tell it apart from the pile they were about to create"
            )
            raise UsageError(msg)
        if len(text) > MAX_BODY_CHARS:
            msg = f"description is {len(text)} chars; it is a label, not a note — write the detail as a note"
            raise UsageError(msg)
        if self.get_subject(subject) is not None:
            msg = (
                f"subject {subject!r} already exists; `cairn notes {subject}` reads it, and a note is how you add to it"
            )
            raise UsageError(msg)
        self._touch(author)
        self._db.execute(
            "INSERT INTO subjects (name, description, created_by, created_at) VALUES (?, ?, ?, ?)",
            (subject, text, author, now()),
        )
        return self._require_subject(subject)

    def archive_subject(self, name: str, author: str, *, reopen: bool = False) -> SubjectSummary:
        """Close a pile to new notes, or open it again. Reading is never affected.

        **Archiving hides and refuses; it never deletes and never conceals.** The
        notes stay, `cairn notes <subject>` still reads them in full, and the pile
        is still in the index under `--archived`. What it stops is the index
        growing without bound as finished runs accumulate — which is the second
        way subject sprawl arrives, after near-duplicate names.

        Reversible on purpose, and that is why it refuses new notes rather than
        merely hiding: a finished run that turns out to have one more thing to say
        should make somebody type `--reopen` and thereby notice they are reopening
        finished work, rather than quietly appending to it.

        No ownership check. cairn has no authentication, so one would be a
        pretence — I3 — and the row records who did it, which is the accountability
        that is actually available.
        """
        if self.get_agent(author) is None:
            msg = f"unknown author {author!r}; register before archiving a subject"
            raise UsageError(msg)
        subject = normalize_subject(name)
        pile = self._require_subject(subject)
        if not reopen and pile.open_questions:
            # Refused rather than warned, because archiving takes the pile out of
            # the index and the index is ordered by open questions — the one
            # column whose whole job is to stop a loop being forgotten. Closing
            # finished work should mean looking at what is still open on it. The
            # escape is always available and is one command: an answer of "no
            # longer relevant, run closed" settles a question perfectly well.
            plural = "question" if pile.open_questions == 1 else "questions"
            msg = (
                f"subject {subject!r} has {pile.open_questions} open {plural}, so archiving it would hide "
                f"them from the index.\n"
                f"  see them: cairn notes {subject} --open\n"
                f'  close one: cairn settle <id> "<what you found, or why it no longer matters>"'
            )
            raise UsageError(msg)
        self._touch(author)
        self._db.execute(
            "UPDATE subjects SET archived_at = ?, archived_by = ? WHERE name = ?",
            (None if reopen else now(), None if reopen else author, subject),
        )
        return self._require_subject(subject)

    def get_subject(self, name: str) -> SubjectSummary | None:
        """Return one subject with its counts, or None."""
        return next((s for s in self.subjects(archived=True) if s.subject == normalize_subject(name)), None)

    # -- internals ------------------------------------------------------------

    def _require_subject(self, subject: str) -> SubjectSummary:
        found = self.get_subject(subject)
        if found is None:
            raise UsageError(_no_such_subject(subject, self._subject_names()))
        return found

    def _subject_names(self) -> list[str]:
        return [r["name"] for r in self._db.execute("SELECT name FROM subjects ORDER BY name").fetchall()]

    def _touch(self, name: str) -> None:
        """Record that `name` was heard from just now.

        `last_seen` used to move only at registration, which made it mean
        `last_registered`. Measured against a live hub: a peer that had sent
        eight messages over twenty-five minutes still advertised the moment it
        joined. That is misleading to a human reading `peers`, and useless to
        anything trying to judge whether a name is still held by a live session
        — which the takeover rule in `register` now depends on.

        Silently a no-op for an unregistered name. Callers reach here through
        paths that already validated the agent, or through `ack`, where refusing
        to record a cursor because the agent row is missing would be worse than
        not updating a timestamp.
        """
        self._db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now(), name))

    def _head(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(seq), 0) AS head FROM messages").fetchone()
        return int(row["head"])

    def _cursor(self, agent: str) -> int:
        row = self._db.execute("SELECT last_acked_seq FROM cursors WHERE agent = ?", (agent,)).fetchone()
        return int(row["last_acked_seq"]) if row else 0


def _agent_from_row(row: sqlite3.Row) -> Agent:
    return Agent(
        name=row["name"],
        machine=row["machine"],
        cwd=row["cwd"],
        capabilities=tuple(json.loads(row["capabilities"])),
        session_id=row["session_id"],
        registered_at=row["registered_at"],
        last_seen=row["last_seen"],
    )


NEAREST_SHOWN = 3
"""How many candidate subjects a refusal offers. Three fits on a line and reads."""

SUBJECTS_SHOWN = 8
"""How many existing subjects a refusal lists when it has no better guess."""


def _nearest(subject: str, names: Sequence[str]) -> list[str]:
    """Return the subjects a mistyped one most likely meant.

    **Substring before edit distance, and that order is from the measured case.**
    `difflib` scores `441` against `soak-441` at 0.55, under any cutoff loose
    enough to be useful, because most of the candidate is the part the writer left
    out. Yet a bare run number is exactly what somebody types when the pile is
    filed under a longer name, and the reverse — typing `rig-a/chamber` when only
    `rig-a` exists — is the same shape with the containment the other way round.
    Both are substring hits, so substring goes first and `difflib` picks up the
    genuine typos underneath it.
    """
    hits = [name for name in names if subject in name or name in subject]
    for name in difflib.get_close_matches(subject, names, n=NEAREST_SHOWN, cutoff=0.6):
        if name not in hits:
            hits.append(name)
    return hits[:NEAREST_SHOWN]


def _no_such_subject(subject: str, names: Sequence[str]) -> str:
    """Return the refusal a writer meets when the pile does not exist yet.

    **This text is the feature.** Requiring subjects to be opened deliberately
    only prevents sprawl if the refusal tells the writer what already exists —
    otherwise it is a speed bump that ends in the same new pile one command later.
    So it guesses, and when it cannot guess it lists, and it always prints the
    exact command with the name already in it.

    Every value here is a normalized subject, so it cannot contain whitespace and
    cannot open a line of its own — the one place in this file where peer-authored
    text reaches a message, and the reason `normalize_subject` refuses whitespace
    outright rather than folding it. See `render.oneline`.
    """
    opening = (
        f"no subject {subject!r}. Subjects are opened deliberately, so that four spellings of one run "
        f"do not become four piles nobody can find."
    )
    lines = [opening]
    near = _nearest(subject, names)
    if near:
        lines.append(f"  did you mean: {', '.join(near)}")
    elif names:
        shown = ", ".join(names[:SUBJECTS_SHOWN])
        more = f" (+{len(names) - SUBJECTS_SHOWN} more)" if len(names) > SUBJECTS_SHOWN else ""
        lines.append(f"  subjects that exist: {shown}{more}")
    lines.append(f'  open it if it is genuinely new: cairn subject {subject} "<one line saying what it is>"')
    lines.append("  see them all, with what each is for: cairn notes")
    return "\n".join(lines)


def _like_escape(text: str) -> str:
    r"""Neutralise SQL `LIKE` wildcards so a search for `100%` finds `100%`.

    Paired with `ESCAPE '\'` at every call site. Without it a body containing a
    literal `%` is unsearchable and a search *for* `%` matches the whole table,
    which reads as a broken index rather than as a quoting rule.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _summary_from_row(row: sqlite3.Row) -> SubjectSummary:
    return SubjectSummary(
        subject=row["subject"],
        notes=int(row["notes"]),
        open_questions=int(row["open_questions"]),
        last_at=row["last_at"],
        description=row["description"],
        created_by=row["created_by"],
        archived_at=row["archived_at"] or "",
    )


def _note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        id=int(row["id"]),
        subject=row["subject"],
        author=row["author"],
        body=row["body"],
        question=bool(row["question"]),
        settles=int(row["settles"]) if row["settles"] is not None else None,
        supersedes=int(row["supersedes"]) if row["supersedes"] is not None else None,
        artifacts=tuple(Artifact.from_json(a) for a in json.loads(row["artifacts"])),
        created_at=row["created_at"],
        deleted_at=row["deleted_at"] or "",
        deleted_by=row["deleted_by"] or "",
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        seq=int(row["seq"]),
        kind=row["kind"],
        sender=row["sender"],
        recipient=row["recipient"],
        body=row["body"],
        correlation_id=row["correlation_id"],
        artifacts=tuple(Artifact.from_json(a) for a in json.loads(row["artifacts"])),
        created_at=row["created_at"],
    )
