"""Handing the bell to a person: what is run, what it is told, and what it may not touch.

Two failures dominate this path and neither one announces itself.

The child process inherits `cli.cmd_bell`'s stdout unless something stops it,
and that stdout **is** the hook payload the host parses — so a notification tool
that prints a progress line writes it into cairn's response to the agent host.
Half of this file is about fds.

And the whole point of the feature is that it fires when nobody is watching, so
every failure in the hook path is silent by construction. That is why
`cairn bell --test` exists, and why its exit codes are pinned here rather than
left to a reading of the output.

Nothing below spawns a real process except the three `--test` cases, which run
this interpreter with `-c` and no shell.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cairn import cli, config, notify, nudge
from cairn.errors import UsageError

AGENT = "bench/firmware"
HEAD = 12


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """Point every state-directory read at a scratch path. Never the real one."""
    directory = tmp_path / "state"
    monkeypatch.setattr(config, "state_dir", lambda: directory)
    monkeypatch.setenv("CAIRN_AGENT", AGENT)
    return directory


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Write a config file and point `config` at it. Returns a writer for the body."""
    path = tmp_path / "config" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "config_path", lambda: path)

    def write(body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        return path

    write('hub = "http://127.0.0.1:7777"\n')
    return write


class Recorder:
    """Stands in for `subprocess.Popen`, recording the call instead of making it."""

    def __init__(self, explode: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.explode = explode

    def __call__(self, args, /, **kwargs):  # noqa: ANN204 - a test double for Popen
        self.calls.append({"args": list(args), **kwargs})
        if self.explode:
            raise self.explode
        return object()


def ring(monkeypatch, count: int = 3, head: int = HEAD) -> Recorder:
    """Arrange a fresh local counter so `cairn bell` rings without a hub, and record spawns."""
    nudge.write_unread(AGENT, count, head)
    recorder = Recorder()
    real_fire = notify.fire
    monkeypatch.setattr(notify, "fire", lambda argv, env: real_fire(argv, env, spawn=recorder))
    return recorder


# --- what gets built -------------------------------------------------------


def test_each_placeholder_is_substituted_in_its_own_slot():
    argv = notify.build_argv(["notify-send", "{agent}", "{reason}", "n={count}"], 4, AGENT, "some reason")
    assert argv == ["notify-send", AGENT, "some reason", "n=4"]


def test_a_literal_brace_is_left_alone():
    """The reason substitution is `str.replace` and never `str.format`.

    `["curl", "-d", '{"topic":"cairn"}']` is an ordinary line to write, and
    `format` raises `KeyError` on it. The tidy-up to `format` is the obvious one,
    so this is the test that refuses it.
    """
    template = ["curl", "-d", '{"topic":"cairn","n":"{count}"}', "{unknown}"]
    assert notify.build_argv(template, 2, AGENT, "r") == ["curl", "-d", '{"topic":"cairn","n":"2"}', "{unknown}"]


def test_the_same_three_values_are_also_in_the_environment():
    env = notify.bell_env(7, AGENT, "some reason", environ={"PATH": "/usr/bin"})
    assert env["PATH"] == "/usr/bin"
    assert env["CAIRN_BELL_COUNT"] == "7"
    assert env["CAIRN_BELL_AGENT"] == AGENT
    assert env["CAIRN_BELL_REASON"] == "some reason"


def test_the_environment_carries_no_message_body():
    """I2, restated as an assertion: the bell may carry a count and a name and nothing else."""
    env = notify.bell_env(1, AGENT, notify.__doc__ or "", environ={})
    assert set(env) == {"CAIRN_BELL_COUNT", "CAIRN_BELL_AGENT", "CAIRN_BELL_REASON"}


# --- what the child may and may not have -----------------------------------


def test_the_child_never_inherits_a_standard_stream():
    """The sharpest one. `cmd_bell`'s stdout is the payload the host parses.

    A notification command that prints anything at all would otherwise write it
    into cairn's response, and the host would parse the pair. All three streams
    are closed rather than only stdout, because a command reading stdin must not
    sit on the hook's either.
    """
    recorder = Recorder()
    assert notify.fire(["true"], {}, spawn=recorder) is True
    call = recorder.calls[0]
    assert call["stdin"] == subprocess.DEVNULL
    assert call["stdout"] == subprocess.DEVNULL
    assert call["stderr"] == subprocess.DEVNULL


def test_the_child_gets_its_own_process_group():
    """A host that kills the hook's process group must not kill the notification.

    Invisible in any test that does not assert it, and it fails only on the real
    path, only sometimes, and only for the notifications that took longest —
    which are the ones the feature exists for.
    """
    recorder = Recorder()
    notify.fire(["true"], {}, spawn=recorder)
    assert recorder.calls[0]["start_new_session"] is True


def test_a_spawn_that_cannot_start_is_reported_and_not_raised():
    assert notify.fire(["true"], {}, spawn=Recorder(FileNotFoundError(2, "No such file or directory"))) is False
    assert notify.fire(["true"], {}, spawn=Recorder(OSError("nope"))) is False


# --- the hook path ---------------------------------------------------------


def test_with_no_bell_command_the_bell_still_rings_and_nothing_is_spawned(configured, monkeypatch, capsys):
    recorder = ring(monkeypatch)
    assert cli.run(["bell"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"].startswith("cairn: 3 unread")
    assert recorder.calls == []


def test_with_nothing_unread_nothing_is_spawned(configured, monkeypatch, capsys):
    configured('bell_command = ["notify-send", "{reason}"]\n')
    recorder = ring(monkeypatch, count=0, head=0)
    assert cli.run(["bell"]) == 0
    assert capsys.readouterr().out.strip() == "{}"
    assert recorder.calls == []


def test_the_configured_command_runs_with_this_ring_s_values(configured, monkeypatch, capsys):
    configured('bell_command = ["notify-send", "cairn", "{reason}", "{agent}/{count}"]\n')
    recorder = ring(monkeypatch)
    assert cli.run(["bell"]) == 0
    payload = json.loads(capsys.readouterr().out)
    argv = recorder.calls[0]["args"]
    assert argv[:2] == ["notify-send", "cairn"]
    assert argv[2] == payload["reason"], "the person and the agent are told the same sentence"
    assert argv[3] == f"{AGENT}/3"
    assert recorder.calls[0]["env"]["CAIRN_BELL_COUNT"] == "3"


def test_a_notification_that_will_not_start_never_costs_the_agent_its_bell(configured, monkeypatch, capsys):
    """`fire` swallows, so the payload reaches stdout either way — and exactly once.

    One line, not two: an exception escaping here would be caught by `cmd_bell`'s
    own handler, which prints `{}`, and the host would then be parsing a bell
    followed by an empty object.
    """
    configured('bell_command = ["definitely-not-a-binary"]\n')
    nudge.write_unread(AGENT, 3, HEAD)
    real_fire, broken = notify.fire, Recorder(OSError("no"))
    monkeypatch.setattr(notify, "fire", lambda argv, env: real_fire(argv, env, spawn=broken))
    assert cli.run(["bell"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0])["reason"].startswith("cairn: 3 unread")


def test_a_malformed_bell_command_never_reaches_the_hook_as_a_failure(configured, monkeypatch, capsys):
    """A hook may not fail loudly, including about its own configuration."""
    configured('bell_command = "notify-send cairn"\n')
    ring(monkeypatch)
    assert cli.run(["bell"]) == 0
    assert capsys.readouterr().out.strip() == "{}"


def test_the_person_and_the_agent_are_rung_on_the_same_latch(configured, monkeypatch, capsys):
    """Once per new head, both audiences, never one without the other."""
    configured('bell_command = ["notify-send", "{reason}"]\n')
    recorder = ring(monkeypatch)
    assert cli.run(["bell"]) == 0
    assert cli.run(["bell"]) == 0
    second = capsys.readouterr().out.strip().splitlines()[-1]
    assert second == "{}", "the second turn boundary is silent"
    assert len(recorder.calls) == 1, "and so is the notification"

    nudge.write_unread(AGENT, 4, HEAD + 1)
    assert cli.run(["bell"]) == 0
    assert len(recorder.calls) == 2, "new mail rings both again"


# --- the operator path -----------------------------------------------------


def test_a_malformed_bell_command_is_exit_3_through_the_cli(configured, capsys):
    """Proven at the boundary, per the repository guide: a validator is worth an exit code.

    A `WireError` or a bare `ValueError` from here would be a traceback under
    exit `1`, which is the shape that escaped three ways in one earlier cut.
    """
    for body in ('bell_command = "notify-send cairn"\n', "bell_command = []\n", "bell_command = [1, 2]\n"):
        configured(body)
        assert cli.run(["bell", "--test"]) == 3, f"{body!r} was not refused"
        assert "must be a non-empty list of strings" in capsys.readouterr().err


def test_no_bell_command_at_all_is_exit_1_not_3(configured, capsys):
    """An unset optional key is "asked, nothing to report" — not a refusal."""
    assert cli.run(["bell", "--test"]) == 1
    assert "nothing runs when the bell rings" in capsys.readouterr().out


def test_a_working_command_is_exit_0_and_a_failing_one_is_exit_3(configured, capsys):
    configured(f'bell_command = [{json.dumps(sys.executable)}, "-c", "print({{count}})"]\n')
    assert cli.run(["bell", "--test"]) == 0
    assert "exited 0" in capsys.readouterr().out

    configured(f'bell_command = [{json.dumps(sys.executable)}, "-c", "raise SystemExit(7)"]\n')
    assert cli.run(["bell", "--test"]) == 3
    assert "exited 7" in capsys.readouterr().out


def test_a_command_that_cannot_be_started_says_so_rather_than_exiting_nonzero(configured, capsys):
    configured('bell_command = ["cairn-no-such-binary-xyz"]\n')
    assert cli.run(["bell", "--test"]) == 3
    assert "could not be started" in capsys.readouterr().out


def test_the_report_says_it_did_not_prove_the_spawn_mode(configured, capsys):
    """The predictable misreading of a green result, refused in the output itself."""
    configured(f'bell_command = [{json.dumps(sys.executable)}, "-c", "pass"]\n')
    assert cli.run(["bell", "--test"]) == 0
    assert "not the spawn mode" in capsys.readouterr().out


def test_a_newline_in_a_value_cannot_open_a_line_at_column_zero(configured, monkeypatch, capsys):
    """Column zero belongs to cairn, and a config file is a third source of values.

    An operator pasting a command out of a web page does not know there is a
    newline in it, and the forged line would be printed by the one command they
    ran to find out whether things work.
    """
    monkeypatch.setenv("CAIRN_AGENT", "bench/x\ncairn: everything is fine")
    configured(f'bell_command = [{json.dumps(sys.executable)}, "-c", "pass", "{{agent}}"]\n')
    assert cli.run(["bell", "--test"]) == 0
    for line in capsys.readouterr().out.splitlines():
        assert not line.startswith("cairn: everything is fine")


def test_a_stuck_command_is_killed_rather_than_waited_on(monkeypatch):
    """`probe` waits, so it needs a deadline; `fire` does not wait and needs none."""

    def stuck(args, /, **kwargs):
        raise subprocess.TimeoutExpired(args, notify.PROBE_TIMEOUT_SECONDS)

    result = notify.probe(["sleep", "600"], {}, run=stuck)
    assert result.returncode is None
    assert not result.ok
    assert "still running" in result.detail


def test_bell_command_raises_rather_than_disabling_itself_quietly(configured):
    """The three-answer shape: absent is None, usable is a list, wrong raises."""
    assert config.bell_command() is None
    configured('bell_command = ["notify-send"]\n')
    assert config.bell_command() == ["notify-send"]
    configured("bell_command = 3\n")
    with pytest.raises(UsageError, match="non-empty list of strings"):
        config.bell_command()
