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
from cairn.wire import BROADCAST, Agent, Artifact, Message, MessageKind, now

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

    def register(self, agent: Agent) -> Agent:
        """Add or refresh an agent. First registration parks the cursor at the head."""
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

    def ack(self, agent: str, seq: int) -> int:
        """Advance the cursor to `seq` and return where it now sits."""
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

    def register(self, agent: Agent) -> Agent:
        """Upsert the row, preserving `registered_at`, and park a new name at the head."""
        existing = self.get_agent(agent.name)
        stamped = Agent(
            name=agent.name,
            machine=agent.machine,
            cwd=agent.cwd,
            capabilities=agent.capabilities,
            session_id=agent.session_id,
            registered_at=existing.registered_at if existing else agent.registered_at,
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
        return stamped

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
        """Insert the message, refusing an unknown sender or a misaddressed recipient."""
        if self.get_agent(sender) is None:
            msg = f"unknown sender {sender!r}; register before sending"
            raise UsageError(msg)
        if recipient != BROADCAST and self.get_agent(recipient) is None:
            known = ", ".join(a.name for a in self.peers()) or "nobody yet"
            msg = f"unknown recipient {recipient!r}; registered agents are: {known}"
            raise UsageError(msg)
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
        """Select what is addressed to `agent` past its cursor, never its own sends."""
        rows = self._db.execute(
            """SELECT * FROM messages
               WHERE seq > (SELECT last_acked_seq FROM cursors WHERE agent = ?)
                 AND (recipient = ? OR recipient = ?)
                 AND sender != ?
               ORDER BY seq LIMIT ?""",
            (agent, agent, BROADCAST, agent, limit),
        ).fetchall()
        return [_message_from_row(r) for r in rows]

    def ack(self, agent: str, seq: int) -> int:
        """Move the cursor forward only: a late ack for old mail cannot rewind it."""
        self._db.execute(
            """INSERT INTO cursors (agent, last_acked_seq) VALUES (?, ?)
               ON CONFLICT(agent) DO UPDATE SET last_acked_seq = MAX(last_acked_seq, excluded.last_acked_seq)""",
            (agent, seq),
        )
        row = self._db.execute("SELECT last_acked_seq FROM cursors WHERE agent = ?", (agent,)).fetchone()
        return int(row["last_acked_seq"])

    # -- internals ------------------------------------------------------------

    def _head(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(seq), 0) AS head FROM messages").fetchone()
        return int(row["head"])


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
