"""Claude Code adapter: skill location, turn-boundary hooks, local session state.

Four product-specific facts live here, all of them measured on 2.1.220 rather
than assumed.

**Skills load from `~/.claude/skills/<name>/SKILL.md`.**

**A `Stop` hook returning `{"decision": "block", "reason": ...}` gets another
turn**, with `reason` delivered as a user message. That is the bell. It carries
a count and an instruction to run `cairn inbox` — never the message itself.
Putting peer text there was tried and the model rejected it as prompt injection,
which was the correct response: hook text arrives with no provenance and no way
for the reader to tell who wrote it. See CLAUDE.md, invariant I1.

**A `SessionStart` hook needs a different envelope for the same sentence**, and
this is the fact the first two cuts of the bell did not know. `decision` is a
`Stop` mechanism; on `SessionStart` the host records the payload as a hook error,
puts the text on stderr, and the model never sees it. The shape that arrives is
`{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
…}}`. Measured both ways with marker hooks: the marker in `additionalContext`
came back quoted by the session, the marker in `reason` did not exist as far as
the session could tell. `bell_payload` is where that difference lives, and
`tests/test_packaging_and_adapter.py` fails if a third event is ever installed
without one.

**`~/.claude/sessions/<pid>.json` publishes a live `status`** — observed values
`idle`, `busy`, `waiting`. This is the signal that makes waking an idle session
safe rather than a race, and it is why the nudger is possible at all. It is
undocumented, so `session_states()` returns an empty list rather than raising
when the shape changes: a missing nudge is a delay, a crash is an outage.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BELL_COMMAND = "cairn bell"
"""Both hooks shell out to this. It exits silently when there is no mail."""

SESSION_ID_ENV = "CLAUDE_SESSION_ID"
"""This product exports its session id here; other products will not."""

TURN_BOUNDARY = "Stop"
"""The event that fires when a turn ends."""

SESSION_START = "SessionStart"
"""The event that fires when a session opens, including on resume."""

HOOK_EVENT_KEY = "hook_event_name"
"""Where the host names the event, in the JSON it writes to the hook's stdin."""


def session_id() -> str | None:
    """Return the id of the session invoking us, if this product publishes one.

    Best effort by design. The id is a breadcrumb for a human reading `peers`,
    never an address — cairn addresses agents by the name they registered, so a
    product that exports nothing costs nothing.
    """
    return os.environ.get(SESSION_ID_ENV) or None


def skills_dir() -> Path:
    """Return where this product loads user skills from."""
    return Path.home() / ".claude" / "skills" / "cairn"


def settings_path() -> Path:
    """Return the user-level settings file this product reads."""
    return Path.home() / ".claude" / "settings.json"


def hook_config() -> dict[str, object]:
    """Return the hook block to merge into settings.

    `Stop` rings the bell at every turn boundary. `SessionStart` drains whatever
    arrived while the session was down — the same bell, at the other end of the
    gap. The same command on both, because the event names itself on stdin and
    `bell_payload` shapes the answer to it.
    """
    entry = [{"hooks": [{"type": "command", "command": BELL_COMMAND}]}]
    return {TURN_BOUNDARY: entry, SESSION_START: entry}


def hook_event(hook_input: str) -> str | None:
    """Return the event the host says invoked us, or None if it said nothing.

    Nothing here raises. A hook that dies takes a turn with it, and every caller
    of this has a usable answer for "no idea" — see `bell_payload`.
    """
    try:
        payload = json.loads(hook_input)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    event = payload.get(HOOK_EVENT_KEY)
    return event if isinstance(event, str) and event else None


def bell_payload(hook_input: str, reason: str) -> dict[str, object]:
    """Return what to print so `reason` actually reaches the reader.

    The envelope is per-event and getting it wrong is **silent**: the host
    accepts the payload, records it, and shows the model nothing. That is how
    the `SessionStart` bell spent three cuts ringing into a void — and worse
    than into a void, because `cli.cmd_bell` latches on what it has announced,
    so the undelivered ring also silenced the next `Stop` boundary, which is the
    one that works. A session opening onto a backlog was told nothing at all.

    Unknown events fall back to the turn-boundary envelope rather than to
    silence. That is the shape a hand-run `cairn bell` should print, and being
    told twice is a smaller failure than not being told — see invariant I2,
    which is about who decides when to read, not about how tidy the reminder is.
    """
    if hook_event(hook_input) == SESSION_START:
        return {"hookSpecificOutput": {"hookEventName": SESSION_START, "additionalContext": reason}}
    return {"decision": "block", "reason": reason}


def merge_hooks(settings: dict[str, object]) -> dict[str, object]:
    """Return `settings` with cairn's hooks added, leaving other hooks intact.

    Idempotent: installing twice does not produce two bells.
    """
    merged = dict(settings)
    hooks = dict(merged.get("hooks") or {})  # type: ignore[arg-type]
    for event, entries in hook_config().items():
        existing = list(hooks.get(event) or [])  # type: ignore[arg-type]
        if not any(BELL_COMMAND in json.dumps(e) for e in existing):
            existing.extend(entries)  # type: ignore[arg-type]
        hooks[event] = existing
    merged["hooks"] = hooks
    return merged


def remove_hooks(settings: dict[str, object]) -> dict[str, object]:
    """Return `settings` with cairn's hooks taken out and everyone else's left alone.

    The inverse of `merge_hooks`, and it exists for a reason that is not
    symmetry: hooks are the one thing cairn writes into a file the user owns and
    shares with other tools. Backing that out should be a command, not an
    instruction to hand-edit JSON — and hand-editing is exactly where somebody
    deletes a neighbour's hook by accident.

    Empty containers are pruned on the way out, so uninstalling from a file that
    had no other hooks leaves no `"hooks": {}` behind to puzzle over later.
    """
    merged = dict(settings)
    hooks = dict(merged.get("hooks") or {})  # type: ignore[arg-type]
    for event in list(hooks):
        kept = []
        for entry in hooks[event] or []:  # type: ignore[union-attr]
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            inner = [h for h in entry.get("hooks") or [] if BELL_COMMAND not in json.dumps(h)]
            if inner:
                kept.append({**entry, "hooks": inner})
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if hooks:
        merged["hooks"] = hooks
    else:
        merged.pop("hooks", None)
    return merged


def sessions_dir() -> Path:
    """Return the directory where this product publishes live session state."""
    return Path.home() / ".claude" / "sessions"


def session_states() -> list[dict[str, object]]:
    """Return live session records, best effort.

    Undocumented format, so every failure mode degrades to "no information".
    A caller that gets an empty list should conclude nothing, not "no sessions".
    """
    directory = sessions_dir()
    if not directory.is_dir():
        return []
    states: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            states.append(record)
    return states


def sessions_for_cwd(cwd: Path) -> list[dict[str, object]]:
    """Return every live session record rooted at `cwd`.

    Plural because one directory really can hold several. Observed on a working
    machine: two records for the same checkout, one `busy` and one publishing no
    status at all. Each record carries its own `name`, so they are separable —
    the caller just has to be told there is a choice to make.
    """
    target = str(cwd.resolve())
    return [r for r in session_states() if str(r.get("cwd") or "") == target]


def session_for_cwd(cwd: Path) -> dict[str, object] | None:
    """Return the most usable live session rooted at `cwd`, if there is one.

    "Most usable" rather than "first", because the first is whatever the glob
    happened to sort earliest — which is a filename, not a fact about the
    session. When a directory holds several, that ordering decided which pane a
    nudge would be typed into, and it could pick a record that publishes no
    status over one sitting idle and ready.

    Preference order: a live process publishing a status we recognise, then any
    live process, then whatever is left. Still a guess when several qualify —
    `cli._watches` says so out loud rather than pretending otherwise.
    """
    records = sessions_for_cwd(cwd)
    if not records:
        return None
    return max(records, key=_usability)


def _usability(record: dict[str, object]) -> tuple[int, int]:
    """Rank a record: recognised status beats live-but-silent beats stale."""
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    alive = 1 if pid > 0 and _alive(pid) else 0
    known = 1 if str(record.get("status") or "") in KNOWN_STATES else 0
    return (alive, known)


KNOWN_STATES = frozenset({"idle", "busy", "waiting"})
"""Status values observed in the wild. Anything else normalises to unknown."""


def _alive(pid: int) -> bool:
    """Return whether a process id is still running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists, owned by someone else
    return True


def session_state(cwd: Path) -> str | None:
    """Return a normalised state for the session rooted at `cwd`.

    One of `"idle"`, `"busy"`, `"waiting"`, or `None` for "no idea". This is the
    `SessionStateReader` the nudger injects, and it is the reason the nudger
    itself stays vendor-free.

    Two ways to get `None` that both matter:

    **The record is stale.** These files outlive the process that wrote them, so
    a crashed session leaves behind a record that still says `idle`. Typing into
    the pane a dead session used to own is the worst outcome this whole path can
    produce, so a record whose pid is gone reports nothing at all.

    **The status is a word we do not recognise.** The field is undocumented and
    may gain values. An unknown state must never be treated as safe to type
    into — see `nudge.WAKEABLE_STATES`, which allows exactly one value.
    """
    record = session_for_cwd(cwd)
    if record is None:
        return None
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or not _alive(pid):
        return None
    status = str(record.get("status") or "")
    return status if status in KNOWN_STATES else None
