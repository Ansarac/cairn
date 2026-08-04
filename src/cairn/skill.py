"""Finding the bundled skill, from a checkout or from an installed wheel.

Both branches are hot paths. The packaged branch is what a user gets from
`uv tool install`, the source branch is what a contributor gets — and a bug in
either one is invisible to the other. Test both.

Note what is *not* here: where the skill gets installed to. That is a property
of a particular agent product, so it lives in `adapters/`. This module only
knows how to find the file and how to copy it into a directory it is handed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cairn.errors import UsageError

SKILL_NAME = "cairn"

Outcome = Literal["created", "replaced", "unchanged"]


@dataclass(frozen=True, slots=True)
class Installation:
    """What `install_skill` did, so the caller can report which of the three cases it was.

    The line counts are carried rather than the texts because the counts are
    what a reader can act on — "was 129 lines, now 258" identifies a stale copy
    at a glance, and a diff of two skill files is not something a deployment
    command should be printing.
    """

    path: Path
    outcome: Outcome
    previous_lines: int
    lines: int


def locate_skill() -> Path:
    """Return the path to SKILL.md, whether running from a checkout or a wheel."""
    packaged = Path(__file__).parent / "_skill" / "SKILL.md"
    if packaged.is_file():
        return packaged
    source = Path(__file__).parent.parent.parent / "skills" / SKILL_NAME / "SKILL.md"
    if source.is_file():
        return source
    msg = "SKILL.md not found in either the packaged or the source location; this build is broken"
    raise UsageError(msg)


def install_skill(dest_dir: Path) -> Installation:
    """Copy the bundled skill into `dest_dir`, saying which of three cases happened.

    **The overwrite is right and the silence was not.** The whole file is
    cairn's, where `settings.json` has neighbours — so unlike
    `cli.cmd_install_hooks` this keeps no backup and does not merge. What a
    silent overwrite is missing is therefore the report, not a merge.

    Why the report earns its place: `SKILL.md` exists in three copies on a
    working machine — the checkout, the wheel, and the installed one — and they
    drift apart with nothing to say so. One was found 129 lines behind while the
    handoff that should have caught it asserted the install had been refreshed,
    which cost a session a hand-diff of all three at every verification step.
    `docs/design.md` §12 item 16 has the incident.

    **`unchanged` does not write**, and that is the part to keep. It leaves the
    file's mtime saying when the skill last actually changed rather than when
    somebody last ran this command, which is the one piece of evidence available
    to whoever is trying to work out how long a machine has been reading the
    wrong thing.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "SKILL.md"
    wanted = locate_skill().read_text(encoding="utf-8")
    previous = target.read_text(encoding="utf-8") if target.is_file() else None
    if previous == wanted:
        return Installation(target, "unchanged", _lines(previous), _lines(wanted))
    target.write_text(wanted, encoding="utf-8")
    outcome: Outcome = "created" if previous is None else "replaced"
    return Installation(target, outcome, _lines(previous), _lines(wanted))


def _lines(text: str | None) -> int:
    """Line count of a copy that may not exist; absent is 0, which no real skill is."""
    return len(text.splitlines()) if text else 0
