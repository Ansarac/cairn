"""Durability, and the server-side cursor.

The store is the only thing in cairn that knows a message survives a reboot. It
does not know that HTTP exists, which is what lets the transport be replaced
without touching delivery semantics.

Two decisions here carry most of the weight.

**The cursor lives on the server.** A client never remembers where it got to.
An agent can be gone for a week, come back with an empty disk, and `unread()`
still returns exactly what it missed. This is the whole answer to "the peer was
switched off", and it costs one integer per agent.

**A new agent starts at the end, a returning agent does not.** Registering a
name for the first time sets its cursor to the current head, so a fresh session
is not buried under a month of other people's mail. Re-registering the same
name — which is what a restarted session does — leaves the cursor alone, so the
backlog it missed is still waiting. Getting this backwards in either direction
is a bug users will feel immediately.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cairn.errors import UsageError
from cairn.wire import BROADCAST, KINDS, Agent, Arrival, Artifact, Message, MessageKind, Registration, now

_COUNT_ALL = 1_000_000
"""A limit high enough to mean "all of it" when counting a skipped backlog."""

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
CREATE TABLE IF NOT EXISTS cursors (
    agent          TEXT PRIMARY KEY,
    last_acked_seq INTEGER NOT NULL DEFAULT 0
);
"""


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

    def unread(self, agent: str, limit: int = 50) -> list[Message]:
        """Return messages after the agent's cursor, oldest first."""
        ...

    def ack(self, agent: str, seq: int, *, rewind: bool = False) -> int:
        """Move the cursor to `seq` and return where it now sits."""
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
        skipped = len(self.unread(agent.name, limit=_COUNT_ALL)) if moved else 0
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

    def unread(self, agent: str, limit: int = 50) -> list[Message]:
        """Select what is addressed to `agent` past its cursor, never its own sends.

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
        rows = self._db.execute(
            """SELECT * FROM messages
               WHERE seq > (SELECT last_acked_seq FROM cursors WHERE agent = ?)
                 AND (recipient = ? OR recipient = ?)
                 AND sender != ?
               ORDER BY seq LIMIT ?""",
            (agent, agent, BROADCAST, agent, limit),
        ).fetchall()
        return [_message_from_row(r) for r in rows]

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

    # -- internals ------------------------------------------------------------

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
