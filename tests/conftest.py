"""Keep the suite off the developer's real home.

Three test files already redirected `XDG_STATE_HOME` by hand, because they drive
`cairn register` and that writes an identity. Everything else got away with it by
never touching state — until `client.send` started signing, at which point the
first send in any test created a **real signing key** under
`~/.local/state/cairn/keys/`. Observed, not feared: one `uv run pytest` produced
one, mode 0600, on the machine this was written on.

That is worse than untidy on this particular file. The key is per working
directory, so the one a test created is the same one the maintainer's own
`cairn tell` from this checkout would later use — a suite run and a real send
sharing a secret, with the suite having written it. And a test that deleted it
between runs would be silently rotating the key underneath real sends.

So the redirect is autouse and applies to every test rather than being something
each new file has to remember. The three files that set it themselves still work:
they run after this and their `setenv` wins.

`XDG_CONFIG_HOME` goes with it for the same reason one step earlier — nothing in
the suite should be able to read the maintainer's `bell_command`, and a test that
depends on the host's config file is a test that passes here and fails in CI.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_xdg(tmp_path_factory, monkeypatch):
    """Point every XDG root cairn reads at a per-test temporary directory."""
    root = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))


@pytest.fixture
def pile_only():
    """Return a function stripping the anchor line from a `notes` reading.

    Shared rather than duplicated, and the duplication is why. `notes` ends on
    `— hub clock <now>` taken from the response, so comparing two whole readings
    of an **unchanged** pile is a coin weighted by machine load: measured at two
    failures in five full-suite runs, none in twelve runs of the test alone, and
    100% with a 1.1 s gap forced between the reads. The diff is one digit of a
    timestamp, 696 identical characters in.

    There were two copies of that comparison in the suite. The first was found
    and fixed by hand; a sweep for the same shape missed the second because it
    named its variables differently, and it surfaced two commits later in an
    unrelated run. One helper both callers use is the fix for that, not tidiness.

    Strip only that line. The claim being kept is that the *pile* is
    byte-identical — `tests/test_clock.py` is where the anchor's content is
    pinned, and each caller asserts it is still present so a regression deleting
    the footer cannot pass by looking like a clock tick.
    """

    def strip(reading: str) -> str:
        return "\n".join(line for line in reading.splitlines() if not line.startswith("— hub clock "))

    return strip
