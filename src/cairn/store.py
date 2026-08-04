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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    Withdrawal,
    normalize_subject,
    now,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

_TABLES = """
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
    created_at     TEXT NOT NULL,
    retracted_at   TEXT,
    signature      TEXT
);
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
CREATE TABLE IF NOT EXISTS subjects (
    name         TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    archived_at  TEXT,
    archived_by  TEXT,
    described_at TEXT,
    described_by TEXT
);
"""
"""Step 1 of four: the tables, and the file's only statement of the current shape.

These four literals are declared in the order `__init__` runs them, because the
order is the whole of what makes an in-place upgrade work: tables, then the
columns those tables are missing, then everything that *names* a column, then the
data repair. Getting that order wrong once cost a live hub its database — see
`_INDEXES`.

**Nothing here reaches a database that already exists.** `CREATE TABLE IF NOT
EXISTS` against an existing `notes` is a no-op, columns and all, so the column
lists below are a description for a reader and an instruction only on the very
first open. What carries a shape change to a live hub is `_MIGRATIONS`, and the
two have to be kept saying the same thing by hand.
"""

_MIGRATIONS = (
    "ALTER TABLE notes ADD COLUMN supersedes INTEGER REFERENCES notes (id)",
    "ALTER TABLE notes ADD COLUMN deleted_at TEXT",
    "ALTER TABLE notes ADD COLUMN deleted_by TEXT",
    "ALTER TABLE messages ADD COLUMN retracted_at TEXT",
    "ALTER TABLE subjects ADD COLUMN described_at TEXT",
    "ALTER TABLE subjects ADD COLUMN described_by TEXT",
    "ALTER TABLE messages ADD COLUMN signature TEXT",
)
"""Step 2: columns added to a table that already exists, on the same terms as `_BACKFILL`.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that is already there, so a
schema change to `notes` reaches an existing database only through `ALTER TABLE`.
Each of these runs at open and each is expected to fail once the column is there;
the duplicate-column error is the success case on every run after the first. That
is uglier than a version table and it is the shape that cannot get out of step
with the hub in the container, which nobody is going to run a migration against.

The suppression is deliberately blanket rather than matched against the
duplicate-column text, and the cost is worth knowing: a migration that fails for
some *other* reason is skipped in silence, and the next thing to touch that column
is a query. Narrowing it means either matching an error string or introspecting
`PRAGMA table_info`, and both were judged worse than the failure they catch — the
one deployment that matters is a container hub whose only failure mode anybody has
actually seen is refusing to open at all. If a fifth entry here is ever something
other than `ALTER TABLE ... ADD COLUMN`, that judgement expires.

**Every statement must be reachable from the oldest schema still in service, not
just from the one before it.** These run as an unordered set on every open, so a
hub that skipped three releases gets all of them in one pass and there is no chain to
keep in step; what that costs is that no entry may ever depend on an earlier one
having run. `tests/test_upgrade.py` opens a database in every shape cairn has
shipped rather than only the newest, which is the only way that stays true.
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS messages_by_recipient ON messages (recipient, seq);
CREATE INDEX IF NOT EXISTS messages_by_sender ON messages (sender, seq);
CREATE INDEX IF NOT EXISTS notes_by_subject ON notes (subject, id);
CREATE INDEX IF NOT EXISTS notes_by_settles ON notes (settles);
CREATE INDEX IF NOT EXISTS notes_by_supersedes ON notes (supersedes);
"""
"""Step 3, and it is separate from `_TABLES` for one reason: **it names columns.**

An index cannot be built over a column that is not there yet, and on an existing
database the column it wants may only arrive in step 2. `notes_by_supersedes` was
added to the table script by the same commit that added `supersedes` to
`_MIGRATIONS`, and it took the container hub down: `CREATE TABLE IF NOT EXISTS
notes` was a no-op on the old six-column `notes`, the index then raised `no such
column: supersedes`, and because `executescript` abandons the **whole** script at
the first error, nothing after it ran either — including the very migration that
would have added the column. Every restart hit it identically. The hub was
recovered by wiping the volume; rebuilding it from the wire was not possible,
because the store is the only copy.

So the rule is not "indexes go last" as a tidiness convention. It is that anything
in this file which *references* a column — an index today, a view or a trigger
tomorrow — belongs after `_MIGRATIONS`, and anything that *defines* one belongs
before. `_TABLES` holding a single `CREATE INDEX` is enough to rebuild the
crashloop, which is why `tests/test_upgrade.py` asserts it holds none.
"""

_BACKFILL = """
INSERT OR IGNORE INTO subjects (name, description, created_by, created_at)
SELECT n.subject, ?, ?, MIN(n.created_at) FROM notes n GROUP BY n.subject
"""
"""Step 4: give every subject that already has notes a row, once, at open.

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
        signature: str = "",
    ) -> Message:
        """Durably record a message and return it with its assigned sequence.

        `signature` is stored and handed back untouched. The hub does not verify
        it and could not: the key is the sender's and lives on the sender's
        machine. Storing something it cannot check is the whole shape of this —
        the hub carries evidence, the reader runs the check.
        """
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

    def retract(self, seq: int, sender: str) -> Withdrawal:
        """Withhold a message from every mailbox that has not passed it yet."""
        ...

    def prune(self, older_than_days: int) -> tuple[int, int, tuple[str, ...]]:
        """Delete old messages nobody still has unread; return what went, what stayed, and whose."""
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

    def describe_subject(self, name: str, description: str, author: str) -> tuple[SubjectSummary, str]:
        """Correct an existing pile's description, returning it and the text it replaced."""
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
        """Open `path`, creating the schema if it is not there yet and upgrading it if it is old.

        **These four steps are ordered, and the order is load-bearing.** Tables
        first, because a migration needs something to alter. Migrations second,
        because on an existing database that is the only step that can add a
        column. Indexes third, because they name columns and step 2 is where the
        column may have just arrived. The backfill last, because it reads the
        tables all three have finished shaping. Merging steps 1 and 3 back into
        one script is what took the container hub down; `_INDEXES` has the trace.
        """
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_TABLES)
        for statement in _MIGRATIONS:
            with contextlib.suppress(sqlite3.OperationalError):
                self._db.execute(statement)
        self._db.executescript(_INDEXES)
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

    def append(  # noqa: PLR0913, PLR0917 - these are the message schema; collapsing them would hide the contract
        self,
        kind: MessageKind,
        sender: str,
        recipient: str,
        body: str,
        correlation_id: str | None = None,
        artifacts: Sequence[Artifact] = (),
        signature: str = "",
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
            """INSERT INTO messages (kind, sender, recipient, body, correlation_id, artifacts, created_at, signature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                kind,
                sender,
                recipient,
                body,
                correlation_id,
                json.dumps([a.to_json() for a in artifacts]),
                created,
                signature,
            ),
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
            signature=signature,
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
        # `retracted_at IS NULL` sits in the *shared* predicate rather than only on
        # the row query, so it applies to `unread`, `head` and `matching` alike. A
        # withdrawn message that still counted would ring a turn-boundary bell for
        # mail that can never render, and latch the head on a seq nothing will ever
        # deliver — which is docs/design.md §12 item 6's deafness, rebuilt.
        where = """FROM messages
               WHERE seq > (SELECT last_acked_seq FROM cursors WHERE agent = ?)
                 AND (recipient = ? OR recipient = ?)
                 AND sender != ?
                 AND retracted_at IS NULL"""
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

    def retract(self, seq: int, sender: str) -> Withdrawal:
        """Withhold a message from every mailbox that has not passed it yet.

        **A message that has been read is out of the mechanism, and cairn says so
        rather than pretending.** Once a recipient's cursor is past it the text is
        in somebody's context and no protocol reaches it; the honest answer is a
        refusal, which tells the sender the thing they actually need to know — that
        escalating is now their problem. Unsaying it is not on offer. Correcting it
        is, and that is an ordinary message.

        **A broadcast is partial by nature and reports itself that way.** One row,
        many mailboxes, each with its own cursor: some will never see it, some
        already have. "It worked" and "it failed" are both wrong, so the answer is
        **both** lists of names — who was spared and who was too late. It said
        only the second for two cuts, which reads as an answer and is not one: the
        sender's next move is deciding who to go and talk to, and that is the list
        it left out. It is refused only when *no* mailbox can still be spared.

        The cursor is the only signal there is, and it is not perfect: a reader that
        ran `cairn inbox --no-ack` has seen the message and left its cursor behind,
        so this will happily withhold something already on somebody's screen. cairn
        cannot know that, and a retraction that overstated what it achieved would be
        worse than one that occasionally understates it.

        **The body is kept**, unlike `delete_note`. Deleting a note is about text
        that should not exist; retracting is about delivery that should not happen,
        and the sender is owed a record in `cairn sent` of what it pulled back.

        One `UPDATE`, so the window between deciding and doing is not a window: a
        reader cannot ack in the middle of a statement.
        """
        row = self._db.execute("SELECT * FROM messages WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            msg = f"no message {seq} to retract"
            raise UsageError(msg)
        message = _message_from_row(row)
        if message.sender != sender:
            msg = (
                f"seq {seq} was sent by {message.sender}, not by you; only a sender may withdraw its own words. "
                f"To correct somebody else's, send a message saying so"
            )
            raise UsageError(msg)
        if message.retracted:
            msg = f"seq {seq} was already withdrawn on {message.retracted_at}"
            raise UsageError(msg)
        holders = self._holders(message)
        still = tuple(name for name, cursor in holders if cursor < seq)
        gone = tuple(name for name, cursor in holders if cursor >= seq)
        if not still:
            who = ", ".join(gone) or "nobody it was addressed to is registered any more"
            msg = (
                f"too late: seq {seq} has already been read by {who}. It is out of the pipe and out of cairn's "
                f"reach — say what changed in a new message instead"
            )
            raise UsageError(msg)
        self._touch(sender)
        withdrawn = now()
        self._db.execute("UPDATE messages SET retracted_at = ? WHERE seq = ?", (withdrawn, seq))
        return Withdrawal(
            message=replace(message, retracted_at=withdrawn),
            withheld=len(still),
            read_by=gone,
            withheld_from=still,
        )

    def _holders(self, message: Message) -> list[tuple[str, int]]:
        """Return every mailbox this message was addressed to, with where its cursor sits.

        A name for a direct message; everyone but the sender for a broadcast — the
        same predicate `unread` selects on, read from the other end. An agent that
        registered *after* the send has its cursor parked at the head by
        `register`, so it counts as past the message, which is exactly right: it
        was never going to be given it.
        """
        rows = self._db.execute(
            """SELECT c.agent AS agent, c.last_acked_seq AS seq FROM cursors c
               WHERE c.agent != ? AND (? = ? OR c.agent = ?)""",
            (message.sender, message.recipient, BROADCAST, message.recipient),
        ).fetchall()
        return [(r["agent"], int(r["seq"])) for r in rows]

    def prune(self, older_than_days: int) -> tuple[int, int, tuple[str, ...]]:
        """Delete old messages nobody still has unread, and report what stayed.

        **The pipe is not sediment.** A message is addressed to a session and read
        once; notes are what outlive one. So this deletes outright rather than
        leaving tombstones — a tombstone per pruned line of shift traffic would be
        the thing pruning exists to remove, in a smaller font.

        **It cannot take undelivered mail, and that is the whole safety property.**
        "The peer was switched off for a week and got its backlog anyway" is the
        premise of the product; a cleanup that could break it would be worse than
        no cleanup. So the predicate is not age alone: a message goes only when no
        registered mailbox still has a cursor below it. Anything held back is
        counted and reported, because a prune that quietly did less than asked is
        the silent shape this project keeps refusing.

        **And named, not just counted.** The count alone reads as *"2 older
        messages are still unread by somebody"*, which an operator running this on
        a shared hub cannot act on: the instruction it answers is always about a
        particular machine coming back off leave, and "somebody" does not say
        whether that is the machine. An acceptance session ran the command, kept
        the backlog, and had to report that it could confirm *a* backlog was
        preserved and not *the* one.

        **The cutoff is computed here, on the hub's clock**, from a number of days
        rather than an instant. Every `created_at` in this table was stamped by
        this clock, and letting a caller send an absolute cutoff would reintroduce
        exactly the two-clock arithmetic §12 item 12 took out of `peers`.

        Retracted mail is prunable on the same terms as anything else: nobody can
        read it, so nobody's cursor is waiting on it.
        """
        if older_than_days < 1:
            msg = f"--older-than needs at least 1 day, got {older_than_days}; there is no safe way to prune today"
            raise UsageError(msg)
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        cutoff = cutoff.replace("+00:00", "Z")
        # `m.retracted_at IS NULL` inside the hold, not outside it. A withdrawn
        # message is unreadable by construction, so no cursor is waiting on it and
        # nothing is lost by taking it — but a cursor sitting below its seq looks
        # exactly like a cursor waiting for it, and without this clause the one
        # class of message that is guaranteed safe to prune would be the class
        # that never got pruned. Found live, against the docstring above it.
        #
        # `holder` is one string because it is asked in both directions: which
        # messages a mailbox is holding, and which mailboxes are holding one. Two
        # spellings of a predicate this load-bearing would drift, and the drift
        # would show up as a prune that deleted mail while naming nobody — the
        # count and the names disagreeing about the thing the count exists for.
        holder = """m.retracted_at IS NULL
                    AND c.last_acked_seq < m.seq AND c.agent != m.sender
                    AND (m.recipient = ? OR m.recipient = c.agent)"""
        held = f"AND EXISTS (SELECT 1 FROM cursors c WHERE {holder})"  # noqa: S608 - `holder` is a literal; every value is a bound parameter
        scope = (cutoff, BROADCAST)
        kept = int(
            self._db.execute(f"SELECT COUNT(*) AS c FROM messages m WHERE m.created_at < ? {held}", scope).fetchone()[  # noqa: S608 - `held` is a literal; every value is a bound parameter
                "c"
            ]
        )
        cursor = self._db.execute(
            f"SELECT COUNT(*) AS c FROM messages m WHERE m.created_at < ? AND NOT ({held.removeprefix('AND ')})",  # noqa: S608 - ditto
            scope,
        ).fetchone()
        removable = int(cursor["c"])
        # The same predicate read from the mailbox end, before the delete rather
        # than after it — the rows the names come from are about to go.
        holders = self._db.execute(
            f"""SELECT DISTINCT c.agent AS agent FROM cursors c
                WHERE EXISTS (SELECT 1 FROM messages m WHERE m.created_at < ? AND {holder})
                ORDER BY c.agent""",  # noqa: S608 - ditto
            scope,
        ).fetchall()
        self._db.execute(
            f"DELETE FROM messages WHERE seq IN (SELECT m.seq FROM messages m WHERE m.created_at < ? AND NOT ({held.removeprefix('AND ')}))",  # noqa: S608, E501 - ditto
            scope,
        )
        return removable, kept, tuple(r["agent"] for r in holders)

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
        `archive_subject`: cairn cannot tell one agent from another, so a check
        would be a pretence — I3 — and the tombstone names who did it, which is
        the accountability that actually exists. A hub token does not change
        this and was never going to: it is one secret shared by every agent
        machine, so it separates the network from a stranger and never one
        caller from another.
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
        # The tombstone count is the same filter with the liveness clause forced
        # to "deleted", so it can never disagree with the page about what was
        # being looked at — and it counts the same thing in both views.
        #
        # It used to *flip* the clause instead, which is a different sentence: the
        # complement of the page. In the plain view those coincide, so it read
        # correctly for two cuts. Under `deleted=True` the complement is the live
        # notes, and the footnote calling it a deletion count then reported "15
        # notes have been deleted here" over a page showing one tombstone. Three
        # independent acceptance sessions hit it and two said they read it three
        # times; one was midway through a hub tidy-up, where a number like that
        # reads as evidence something has already gone missing on your watch.
        buried = clause.replace(where[0], "n.deleted_at IS NOT NULL", 1)
        removed = int(self._db.execute(f"SELECT COUNT(*) AS c FROM notes n WHERE {buried}", params).fetchone()["c"])  # noqa: S608 - ditto
        rows = self._db.execute(
            f"""SELECT n.*,
                       (SELECT MIN(s.id) FROM notes s WHERE s.settles = n.id AND s.deleted_at IS NULL) AS settled_by,
                       (SELECT MAX(s.id) FROM notes s WHERE s.supersedes = n.id AND s.deleted_at IS NULL)
                           AS superseded_by,
                       -- Whether the pile this sits on is closed. A subject read
                       -- rolls up its children, so an archived child's notes turn
                       -- up inside a live parent's reading with nothing to say the
                       -- pile is finished — and the index does not list it either,
                       -- so the reader has no second place to find out. NULL for a
                       -- note whose subject row predates `subjects`, which reads as
                       -- "not archived" and is right: it cannot have been.
                       (SELECT s.archived_at IS NOT NULL FROM subjects s WHERE s.name = n.subject) AS archived
                FROM notes n WHERE {clause} ORDER BY n.id DESC LIMIT ?""",  # noqa: S608 - ditto
            [*params, limit],
        ).fetchall()
        entries = [
            NoteEntry(
                note=_note_from_row(r),
                settled_by=int(r["settled_by"]) if r["settled_by"] is not None else None,
                superseded_by=int(r["superseded_by"]) if r["superseded_by"] is not None else None,
                archived=bool(r["archived"]),
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
                       -- A description nobody has corrected was written when the pile was
                       -- opened, by whoever opened it. Coalescing says that rather than
                       -- leaving a null to render as "nobody knows", which would make an
                       -- original description look less accountable than a corrected one.
                       COALESCE(s.described_at, s.created_at) AS described_at,
                       COALESCE(s.described_by, s.created_by) AS described_by,
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

    def describe_subject(self, name: str, description: str, author: str) -> tuple[SubjectSummary, str]:
        """Correct the description of a pile that already exists, and return what it replaced.

        **The field the whole command rests on was the one field nothing could
        fix.** `create_subject` argues that what stops a fifth spelling is the
        index saying what each pile is for. Three acceptance sessions across two
        cuts then found the same sentence failing three different ways, and none
        of them had a way out: one wrote *"today's incident leaking into a
        description meant to outlive it"*, one measured a stale one at *"six
        months out, negative — not zero"* because it misroutes rather than merely
        ageing, and one filed the claim it was least sure of — *"Distinct rig from
        chamber-2"* — and noted that unlike a note, a description cannot be
        superseded. A load-bearing field with no correction path is one that gets
        quietly worse for as long as the pile survives.

        **Anyone registered may correct it, not only the author.** `retract` is
        owner-only because it withdraws somebody's words from other people's
        mailboxes; this is the opposite act — the sentence is shared
        infrastructure, and the reader best placed to notice it is wrong is
        precisely the one it just misrouted. A session that spotted a stale
        description left it alone and said why: *"correcting it is a supersede on
        somebody else's note, which is outside what you asked me for."* Making the
        fix need permission is how it stays broken.

        **The old text comes back rather than being kept.** Same rule as a
        takeover: state the loss to the person causing it, at the moment they
        cause it. Storing a chain of former descriptions would make this a wiki,
        and the durable place for "it used to say X and that was wrong" already
        exists — it is a note on the pile.

        This does not conflict with `create_subject` refusing an existing name.
        That refusal is aimed at a writer who does not know the pile exists and
        would silently overwrite a stranger's sentence; this is a separate verb
        that cannot be reached by accident and says whose words it replaced.

        Allowed on an archived pile, because a description is not content. A
        finished run whose label is wrong misleads exactly the person digging
        through old work, who has the least context to catch it.
        """
        if self.get_agent(author) is None:
            msg = f"unknown author {author!r}; register before describing a subject"
            raise UsageError(msg)
        subject = normalize_subject(name)
        pile = self._require_subject(subject)
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
        if text == pile.description:
            msg = f"subject {subject!r} already reads exactly that; nothing to correct"
            raise UsageError(msg)
        self._touch(author)
        self._db.execute(
            "UPDATE subjects SET description = ?, described_at = ?, described_by = ? WHERE name = ?",
            (text, now(), author, subject),
        )
        return self._require_subject(subject), pile.description

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

        No ownership check. cairn cannot tell one agent from another — a hub
        token authenticates the network, not the caller — so one would be a
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

    def _subject_names(self) -> list[tuple[str, bool, str]]:
        """Every subject name, whether it is archived, and what it says it is.

        Archived piles are included on purpose, and this is the one reader of
        the table that must not filter them out. The index hides them because
        it is a list of live work; this is a list of *what already exists*, and
        a pile the writer is not shown is a pile they open a second copy of —
        which is the exact failure `_no_such_subject` is here to prevent.

        The description rides along because the refusal is the moment a writer
        decides which pile they meant, and a bare name cannot answer that when
        there is more than one candidate. See `_no_such_subject`.
        """
        rows = self._db.execute("SELECT name, archived_at, description FROM subjects ORDER BY name").fetchall()
        return [(r["name"], bool(r["archived_at"]), r["description"] or "") for r in rows]

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


def _nearest(subject: str, names: Sequence[str], archived: Collection[str] = ()) -> list[str]:
    """Return the subjects a mistyped one most likely meant.

    **Substring before edit distance, and that order is from the measured case.**
    `difflib` scores `441` against `soak-441` at 0.55, under any cutoff loose
    enough to be useful, because most of the candidate is the part the writer left
    out. Yet a bare run number is exactly what somebody types when the pile is
    filed under a longer name, and the reverse — typing `rig-a/chamber` when only
    `rig-a` exists — is the same shape with the containment the other way round.
    Both are substring hits, so substring goes first and `difflib` picks up the
    genuine typos underneath it.

    **A live pile outranks an archived one**, because only three guesses are
    shown and one of these two can be written to. The sort is stable, so it
    demotes archived hits without disturbing the order above.
    """
    hits = [name for name in names if subject in name or name in subject]
    for name in difflib.get_close_matches(subject, names, n=NEAREST_SHOWN, cutoff=0.6):
        if name not in hits:
            hits.append(name)
    hits.sort(key=lambda name: name in archived)
    return hits[:NEAREST_SHOWN]


def _listed(label: str, names: Sequence[str], limit: int) -> str:
    """Render one `label: a, b, c (+N more)` line of the refusal."""
    shown = ", ".join(names[:limit])
    more = f" (+{len(names) - limit} more)" if len(names) > limit else ""
    return f"  {label}: {shown}{more}"


def _explained(label: str, names: Sequence[str], describing: dict[str, str]) -> list[str]:
    """Render a label and one indented `name  what it is` line per candidate.

    **Descriptions are folded here rather than trusted from the write path.**
    Both writers collapse whitespace, so a stored description cannot open a line
    of its own today — but this function is two modules away from the code that
    guarantees it, and column zero belonging to cairn is not a property worth
    holding at that distance. The collapse is one call and removes the question.
    """
    width = max(len(name) for name in names)
    return [
        f"  {label}:",
        *(f"    {name:<{width}}  {' '.join(describing.get(name, '').split())}".rstrip() for name in names),
    ]


def _no_such_subject(subject: str, names: Sequence[tuple[str, bool, str]]) -> str:
    """Return the refusal a writer meets when the pile does not exist yet.

    **This text is the feature.** Requiring subjects to be opened deliberately
    only prevents sprawl if the refusal tells the writer what already exists —
    otherwise it is a speed bump that ends in the same new pile one command later.
    So it guesses, and when it cannot guess it lists, and it always prints the
    exact command with the name already in it.

    **An archived pile is named, and named on its own line.** It has to be named
    at all, because a writer who is not shown it opens a second copy of it — the
    index may hide archived piles, but this is the one place where the question
    is not "what is live" but "does this already exist". It cannot be named in
    the same breath as a live one either: the writer's next act is a `cairn note`
    that an archived pile refuses, and a suggestion whose only outcome is a
    second refusal teaches a reader to stop reading these. So the line carries
    the command that makes the suggestion usable. Found while staging an
    acceptance run: the list offered an archived pile and the next command
    rejected it, which is `docs/design.md` §12 item 16 defect 6 a third time,
    at a surface that fix did not reach.

    **A guess says what each pile is; a list does not.** Two candidates named and
    nothing else is, in an acceptance session's words, *"a coin flip dressed as
    help"* — the guess ranks on string similarity, which cannot know which pile
    is the one you meant, and *"what disambiguates is the descriptions and dates,
    and those are only in the index."* So the branch a writer is about to act on
    explains itself. The list branch does not: it can run to eight names, and the
    last line of the refusal already points at the surface that has them, saying
    so in those words.

    Worth being precise about why this is safe here and was rejected on
    `cairn subject`. There, a volunteered near-name would arrive beside an
    operator's belief that the pile exists and read as independent confirmation
    of it — *"persuasive prompts don't usually flip a decision; they blur it."*
    Here the writer has already asserted a name that does not exist and the tool
    has to answer. A description makes a wrong guess easier to **reject**, which
    is the opposite pressure.

    The subject strings are normalized, so they cannot contain whitespace and
    cannot open a line of their own — the reason `normalize_subject` refuses
    whitespace outright rather than folding it. Descriptions are not normalized
    that way and are folded by `_explained` instead. See `render.oneline`.
    """
    all_names = [name for name, _, _ in names]
    archived = {name for name, is_archived, _ in names if is_archived}
    describing = {name: text for name, _, text in names}

    opening = (
        f"no subject {subject!r}. Subjects are opened deliberately, so that four spellings of one run "
        f"do not become four piles nobody can find."
    )
    lines = [opening]
    near = _nearest(subject, all_names, archived)
    guessing = bool(near)
    candidates, label, limit = (
        (near, "did you mean", NEAREST_SHOWN) if guessing else (all_names, "subjects that exist", SUBJECTS_SHOWN)
    )
    reopen_label = "archived, so writing needs `cairn subject <name> --reopen` first"
    live = [name for name in candidates if name not in archived]
    closed = [name for name in candidates if name in archived]
    if live:
        lines.extend(_explained(label, live[:limit], describing) if guessing else [_listed(label, live, limit)])
    if closed:
        lines.extend(
            _explained(reopen_label, closed[:limit], describing) if guessing else [_listed(reopen_label, closed, limit)]
        )
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
        described_at=row["described_at"] or "",
        described_by=row["described_by"] or "",
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
        retracted_at=row["retracted_at"] or "",
        signature=row["signature"] or "",
    )
