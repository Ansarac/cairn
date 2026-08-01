"""Claude Code adapter: skill location, turn-boundary hooks, local session state.

Three product-specific facts live here, all of them measured on 2.1.220 rather
than assumed.

**Skills load from `~/.claude/skills/<name>/SKILL.md`.**

**A `Stop` hook returning `{"decision": "block", "reason": ...}` gets another
turn**, with `reason` delivered as a user message. That is the bell. It carries
a count and an instruction to run `cairn inbox` — never the message itself.
Putting peer text there was tried and the model rejected it as prompt injection,
which was the correct response: hook text arrives with no provenance and no way
for the reader to tell who wrote it. See CLAUDE.md, invariant I1.

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
"""The Stop hook shells out to this. It exits silently when there is no mail."""

SESSION_ID_ENV = "CLAUDE_SESSION_ID"
"""This product exports its session id here; other products will not."""


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
    gap.
    """
    entry = [{"hooks": [{"type": "command", "command": BELL_COMMAND}]}]
    return {"Stop": entry, "SessionStart": entry}


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
