"""Which build this is, read back out of the artifact.

The trap this file exists for: **the machine running these tests is the editable
case, and every machine that matters is the git one.** A test that called the
real `read_direct_url` would pass here forever while saying nothing at all about
the branch a peer actually exercises, so every case below is driven off a fake
reader and none of them touches this install.

The second one is quieter. `--version` is the only command that needs any of
this, and `cairn bell` runs at every turn boundary — so there is a test asserting
that an ordinary command does not go looking.
"""

from __future__ import annotations

import json

import pytest

from cairn import __version__, build, cli

GIT = json.dumps(
    {
        "url": "https://github.com/Ansarac/cairn.git",
        "vcs_info": {"vcs": "git", "commit_id": "0014626241fe41cc248a3aa2f0de1bc500af956b"},
    }
)
LOCAL = json.dumps({"url": "file:///home/you/dev/cairn", "dir_info": {}})
EDITABLE = json.dumps({"url": "file:///home/you/dev/cairn", "dir_info": {"editable": True}})


def reader(raw: str | None):
    """Return a `DirectUrlReader` that always answers `raw`."""
    return lambda: raw


def test_a_git_install_names_its_commit():
    """The case every peer machine is in, and the one this machine can never produce."""
    assert build.describe(reader(GIT)) == f"cairn {__version__} (git 0014626)"


def test_a_local_install_names_the_directory_rather_than_a_commit():
    """A checkout has no commit worth printing — the working tree is whatever it is now.

    Saying the directory is more use than a hash that stopped being true at the
    next unstaged edit.
    """
    assert build.describe(reader(LOCAL)) == f"cairn {__version__} (/home/you/dev/cairn)"
    assert build.describe(reader(EDITABLE)) == f"cairn {__version__} (/home/you/dev/cairn, editable)"


def test_a_build_that_cannot_say_falls_back_to_the_bare_version():
    """Not defensive coding: anything not installed by a tool that writes the file."""
    assert build.describe(reader(None)) == f"cairn {__version__}"


@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", "[]", '"a string"', json.dumps({"url": "x"}), json.dumps({"url": "x", "vcs_info": {}})],
)
def test_a_half_answer_is_no_answer(raw):
    """A malformed file reports nothing rather than something partly true.

    This sits on a version line somebody is reading in order to decide whether a
    machine is on the right build, so a guess here is worse than a blank.
    """
    assert build.origin(reader(raw)) is None
    assert build.describe(reader(raw)) == f"cairn {__version__}"


def test_the_reader_never_raises_however_broken_the_install_is():
    """Every failure means the same thing to the caller: this build cannot say."""
    assert build.read_direct_url() is None or isinstance(build.read_direct_url(), str)


def test_an_ordinary_command_never_reads_the_package_metadata(monkeypatch, capsys):
    """`--version` is lazy, and the obvious tidy-up would make it not be.

    `action="version"` takes a finished string, so `version=build.describe()`
    reads dist-info at parser-build time — on every invocation of every command,
    including the bell at every turn boundary. An `importlib.metadata` scan walks
    `sys.path`; `cli.cmd_bell` promises a `stat` and a small read.
    """
    calls = []
    monkeypatch.setattr(build, "describe", lambda: calls.append(1) or f"cairn {__version__} (git deadbee)")

    cli.build_parser()
    assert calls == [], "building the parser must not go looking"

    with pytest.raises(SystemExit) as exit_info:
        cli.run(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"cairn {__version__} (git deadbee)"
    assert calls == [1], "and --version must, exactly once"
