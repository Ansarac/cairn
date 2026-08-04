"""Which build this is, derived from the artifact rather than declared.

`__version__` has read `0.1.0` since the first commit and through every cut
since, because the version names the *package* and nothing bumps it between
releases. The question anybody actually asks — "is that machine running the code
I think it is?" — has therefore had no answer at all, and `docs/design.md` §12
item 19 concluded that the `install-skill` report was the closest thing to a
build check that existed.

It was not. **The installer already wrote down which build it installed.** A
modern pip or uv drops `direct_url.json` beside the package metadata, recording
what the install came from — for a git URL, the resolved commit. It persists on
disk, it needs no reinstall to read, and it cannot drift from the code beside it
the way a hand-maintained string can. This module reads it back.

The finding is a peer's rather than this repository's: an agent on another
machine, asked for one line of `install-skill` output, upgraded first and noticed
that `uv` had named its two builds apart in the install log while `--version`
called them the same thing. Its own framing of why that matters is the right one
— it would have turned the whole exchange into a single question answered in one
line.

Three answers, and none of them is defensive coding — all three are installs this
project actually has:

- a git install, which is how every peer machine is deployed;
- a local path or editable install, which is what a checkout is, and where the
  honest answer is the directory rather than a commit: the working tree is
  whatever it is right now, and saying so is more use than a stale hash;
- nothing at all, for anything not installed by a tool that writes the file.

Imports only stdlib, and takes an injectable reader so the tests do not depend on
how this particular machine happens to be installed — which is a real hazard
here, since the maintainer's machine is the editable case and every peer is the
git one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, distribution

from cairn import __version__

COMMIT_CHARS = 7
"""How much of the commit to print. Enough to paste into `git show`, short enough
to sit in a version line — the same abbreviation git itself defaults to."""

DirectUrlReader = Callable[[], str | None]
"""Returns the raw `direct_url.json`, or None when the build has no metadata."""


def read_direct_url() -> str | None:
    """Return this install's `direct_url.json`, or None if there is not one.

    Never raises. Every failure here — no dist-info, a package installed by
    something older, an unreadable file — means the same thing to the caller:
    this build cannot say where it came from, which is an answer and not an
    error.
    """
    try:
        return distribution("cairn").read_text("direct_url.json")
    except (PackageNotFoundError, OSError, ValueError):
        return None


def origin(read: DirectUrlReader = read_direct_url) -> str | None:
    """Return a short phrase naming what this build was installed from, if anything.

    `git <sha>` for a VCS install, the source directory for a local one, and
    `None` when the artifact says nothing. A malformed file is `None` too: this
    is a convenience on a version line, and a half-parsed answer here would be
    worse than no answer, because somebody would act on it.
    """
    raw = read()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    vcs = parsed.get("vcs_info")
    if isinstance(vcs, dict) and isinstance(vcs.get("commit_id"), str):
        return f"{vcs.get('vcs', 'vcs')} {vcs['commit_id'][:COMMIT_CHARS]}"
    directory = parsed.get("dir_info")
    if isinstance(directory, dict):
        url = parsed.get("url")
        where = str(url).removeprefix("file://") if isinstance(url, str) else "a local directory"
        return f"{where}, editable" if directory.get("editable") else where
    return None


def describe(read: DirectUrlReader = read_direct_url) -> str:
    """Return the `--version` line: the declared version, plus what the artifact says.

    The version literal stays first and unchanged, because it is what a package
    manager and a changelog agree on. What follows is the part that moves.
    """
    came_from = origin(read)
    return f"cairn {__version__} ({came_from})" if came_from else f"cairn {__version__}"
