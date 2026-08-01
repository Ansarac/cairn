"""Finding the bundled skill, from a checkout or from an installed wheel.

Both branches are hot paths. The packaged branch is what a user gets from
`uv tool install`, the source branch is what a contributor gets — and a bug in
either one is invisible to the other. Test both.

Note what is *not* here: where the skill gets installed to. That is a property
of a particular agent product, so it lives in `adapters/`. This module only
knows how to find the file and how to copy it into a directory it is handed.
"""

from __future__ import annotations

from pathlib import Path

from cairn.errors import UsageError

SKILL_NAME = "cairn"


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


def install_skill(dest_dir: Path) -> Path:
    """Copy the bundled skill into `dest_dir` and return where it landed."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "SKILL.md"
    target.write_text(locate_skill().read_text(encoding="utf-8"), encoding="utf-8")
    return target
