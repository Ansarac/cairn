"""The nudger's terminal half, exercised without tmux and without `/proc`.

Nothing here spawns a process or reads a real file under `/proc`: every function
in `cairn.terminal` takes its subprocess runner and its ppid reader as arguments,
and these tests supply both. That is the point of those parameters.

The load-bearing test is `test_send_line_is_two_calls_and_the_second_is_enter`.
It asserts on recorded argv because the two-call split is a measured fact about
bracketed paste (see the module docstring), it looks exactly like something worth
simplifying, and the failure it prevents is silent — the text lands in the pane
and simply never gets submitted.
"""

from __future__ import annotations

import inspect
import subprocess
from dataclasses import dataclass, field

import pytest

from cairn import terminal
from cairn.terminal import (
    PANE_FORMAT,
    TIMEOUT_SECONDS,
    Pane,
    TerminalUnavailable,
    ancestors,
    list_panes,
    pane_for_pid,
    parse_ppid,
    read_ppid,
    send_line,
    tmux_available,
)


@dataclass
class Result:
    """Stands in for `subprocess.CompletedProcess`."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeTmux:
    """A `Runner` that records argv and hands back canned results.

    `raises` simulates the process never starting at all — a missing binary, or a
    timeout.
    """

    results: list[Result] = field(default_factory=list)
    raises: BaseException | None = None
    calls: list[list[str]] = field(default_factory=list)
    kwargs: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs) -> Result:
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.results.pop(0) if self.results else Result()


def ppid_reader(chain):
    """Return a `PpidReader` backed by a plain dict; unknown pids are gone pids."""
    return chain.get


def assert_called_safely(run):
    """Every call must be an argument list, shell-free, and bounded by a timeout."""
    for args, kwargs in zip(run.calls, run.kwargs, strict=True):
        assert isinstance(args, list)
        assert all(isinstance(a, str) for a in args)
        assert kwargs.get("shell") is not True
        assert kwargs["timeout"] == TIMEOUT_SECONDS
        assert kwargs["timeout"] > 0


# --- list_panes ------------------------------------------------------------


def test_list_panes_parses_real_tmux_output():
    run = FakeTmux(results=[Result(stdout="%0 2770592\n%1 2771001\n%12 39\n")])
    assert list_panes(run) == [Pane("%0", 2770592), Pane("%1", 2771001), Pane("%12", 39)]
    assert run.calls == [["tmux", "list-panes", "-a", "-F", PANE_FORMAT]]
    assert_called_safely(run)


def test_list_panes_is_empty_when_the_server_reports_no_panes():
    assert list_panes(FakeTmux(results=[Result(stdout="")])) == []
    assert list_panes(FakeTmux(results=[Result(stdout="\n")])) == []


def test_list_panes_skips_lines_it_cannot_parse():
    """A future tmux field should cost the panes it breaks, not all of them."""
    run = FakeTmux(results=[Result(stdout="%0 100\ngarbage\n%1 notapid\n%2 300 extra\n%3 400\n")])
    assert list_panes(run) == [Pane("%0", 100), Pane("%3", 400)]


def test_a_non_zero_tmux_exit_becomes_terminal_unavailable_carrying_the_stderr():
    run = FakeTmux(results=[Result(returncode=1, stderr="error connecting to /tmp/tmux-1000/default\n")])
    with pytest.raises(TerminalUnavailable) as excinfo:
        list_panes(run)
    message = str(excinfo.value)
    assert "error connecting to /tmp/tmux-1000/default" in message
    assert "exited 1" in message
    assert "list-panes" in message


def test_a_timeout_becomes_terminal_unavailable():
    run = FakeTmux(raises=subprocess.TimeoutExpired(cmd=["tmux"], timeout=TIMEOUT_SECONDS))
    with pytest.raises(TerminalUnavailable, match="did not complete"):
        list_panes(run)


# --- tmux_available --------------------------------------------------------


def test_tmux_available_is_false_when_the_binary_is_missing():
    run = FakeTmux(raises=FileNotFoundError(2, "No such file or directory", "tmux"))
    assert tmux_available(run) is False


def test_tmux_available_is_false_when_no_server_is_running():
    run = FakeTmux(results=[Result(returncode=1, stderr="error connecting to /tmp/tmux-1000/default")])
    assert tmux_available(run) is False


def test_tmux_available_is_true_when_has_session_succeeds():
    run = FakeTmux(results=[Result()])
    assert tmux_available(run) is True
    assert run.calls == [["tmux", "has-session"]]
    assert_called_safely(run)


# --- /proc stat parsing ----------------------------------------------------


def test_proc_stat_parsing_survives_a_comm_with_spaces_and_parentheses():
    """`split()[3]` reads `name)` here. Split on the last `)` instead."""
    assert parse_ppid("1234 (weird (proc) name) S 42 1234 1234 0 -1 4194560\n") == 42


def test_proc_stat_parsing_handles_an_ordinary_line():
    assert parse_ppid("2771365 (sleep) S 2771364 2770592 2770592 34816 2771365 4194304\n") == 2771364


@pytest.mark.parametrize("line", ["", "no parens at all", "1234 (bash)", "1234 (bash) S notapid", "1234 (bash) S"])
def test_proc_stat_parsing_returns_none_rather_than_raising(line):
    assert parse_ppid(line) is None


def test_read_ppid_reads_a_stat_file(tmp_path, monkeypatch):
    """Exercises the glue, against a fake `/proc` root — never the real one."""
    monkeypatch.setattr(terminal, "PROC", tmp_path)
    (tmp_path / "77").mkdir()
    (tmp_path / "77" / "stat").write_text("77 (a (b) c) S 12 77 77 0\n", encoding="utf-8")
    assert read_ppid(77) == 12


def test_read_ppid_is_none_for_a_process_that_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "PROC", tmp_path)
    assert read_ppid(999999) is None


# --- ancestors -------------------------------------------------------------


def test_ancestors_walks_up_to_init():
    chain = ppid_reader({4242: 900, 900: 800, 800: 1, 1: 0})
    assert ancestors(4242, chain) == [4242, 900, 800, 1]


def test_ancestors_stops_when_the_chain_runs_out():
    assert ancestors(7, ppid_reader({})) == [7]


def test_ancestors_terminates_on_a_cycle():
    """A cycle must end the walk, not hang the nudger."""
    assert ancestors(5, ppid_reader({5: 6, 6: 7, 7: 5})) == [5, 6, 7]


def test_ancestors_terminates_on_a_self_referential_pid():
    assert ancestors(9, ppid_reader({9: 9})) == [9]


@pytest.mark.parametrize("pid", [0, -1])
def test_ancestors_of_a_nonsense_pid_is_empty(pid):
    assert ancestors(pid, ppid_reader({0: 0, -1: -1})) == []


def test_ancestors_never_raises_even_if_the_reader_does():
    def broken(pid):
        if pid == 3:
            raise OSError(5, "I/O error")
        return 3

    assert ancestors(2, broken) == [2, 3]


# --- pane_for_pid ----------------------------------------------------------

MEASURED_PANES = "%0 2770592\n%1 5000\n"
"""The pane pid here is the one measured on 2026-08-01; the chain below is too."""


def test_pane_for_pid_finds_the_pane_of_a_deep_descendant():
    run = FakeTmux(results=[Result(stdout=MEASURED_PANES)])
    chain = ppid_reader({2771365: 2771364, 2771364: 2770592, 2770592: 2770591, 2770591: 1, 1: 0})
    assert pane_for_pid(2771365, run, chain) == Pane("%0", 2770592)


def test_pane_for_pid_finds_the_pane_when_the_pid_is_the_pane_root_itself():
    run = FakeTmux(results=[Result(stdout=MEASURED_PANES)])
    assert pane_for_pid(5000, run, ppid_reader({5000: 1, 1: 0})) == Pane("%1", 5000)


def test_pane_for_pid_is_none_when_no_ancestor_is_a_pane_root():
    """A session outside tmux. Normal, and not an error."""
    run = FakeTmux(results=[Result(stdout=MEASURED_PANES)])
    assert pane_for_pid(31337, run, ppid_reader({31337: 400, 400: 1, 1: 0})) is None


def test_pane_for_pid_is_none_when_there_are_no_panes_and_does_not_walk_proc():
    def never(pid):
        pytest.fail("no panes means nothing to match; the ancestor walk is wasted work")

    assert pane_for_pid(31337, FakeTmux(results=[Result(stdout="")]), never) is None


def test_pane_for_pid_still_raises_when_tmux_itself_is_broken():
    run = FakeTmux(results=[Result(returncode=1, stderr="no server running")])
    with pytest.raises(TerminalUnavailable, match="no server running"):
        pane_for_pid(1, run, ppid_reader({}))


# --- send_line -------------------------------------------------------------


def test_send_line_is_two_calls_and_the_second_is_enter():
    """The measured fact, in test form. Bracketed paste swallows a combined Enter.

    If someone collapses this into `send-keys -t %0 'text' Enter`, the text lands
    in the pane's input buffer and is never submitted — visible to nobody but the
    human staring at an un-run prompt. This test is what stops that.
    """
    run = FakeTmux()
    send_line("%0", "cairn inbox: 2 new", run)

    assert len(run.calls) == 2
    text_call, enter_call = run.calls
    assert text_call == ["tmux", "send-keys", "-t", "%0", "-l", "--", "cairn inbox: 2 new"]
    assert enter_call == ["tmux", "send-keys", "-t", "%0", "Enter"]
    assert enter_call[-1] == "Enter"
    assert "Enter" not in text_call
    assert_called_safely(run)


def test_send_line_sends_the_text_literally():
    """`-l` means a line reading `Up` arrives as two characters, not as a keypress."""
    run = FakeTmux()
    send_line("%3", "Up", run)
    assert run.calls[0] == ["tmux", "send-keys", "-t", "%3", "-l", "--", "Up"]


@pytest.mark.parametrize("text", ["two\nlines", "carriage\rreturn", "trailing\n", "\r\n", "a\r\nb"])
def test_send_line_refuses_more_than_one_line(text):
    """Half a line submitted into a live prompt is a command the human did not type."""
    run = FakeTmux()
    with pytest.raises(ValueError, match="single line"):
        send_line("%0", text, run)
    assert run.calls == []


def test_send_line_does_not_press_enter_if_the_text_never_landed():
    run = FakeTmux(results=[Result(returncode=1, stderr="can't find pane: %99")])
    with pytest.raises(TerminalUnavailable, match="can't find pane"):
        send_line("%99", "cairn inbox", run)
    assert len(run.calls) == 1


def test_send_line_reports_a_failure_to_submit():
    run = FakeTmux(results=[Result(), Result(returncode=1, stderr="pane died")])
    with pytest.raises(TerminalUnavailable, match="pane died"):
        send_line("%0", "cairn inbox", run)
    assert len(run.calls) == 2


# --- the injectable API itself ---------------------------------------------


@pytest.mark.parametrize(
    ("func", "parameter", "expected"),
    [
        (tmux_available, "run", subprocess.run),
        (list_panes, "run", subprocess.run),
        (send_line, "run", subprocess.run),
        (pane_for_pid, "run", subprocess.run),
        (pane_for_pid, "ppid_of", read_ppid),
        (ancestors, "ppid_of", read_ppid),
    ],
)
def test_the_seams_have_real_defaults(func, parameter, expected):
    """Injectable for tests, but a caller passes nothing and gets the real thing."""
    assert inspect.signature(func).parameters[parameter].default is expected


def test_pane_is_immutable():
    pane = Pane("%0", 100)
    with pytest.raises(AttributeError):
        pane.pane_id = "%1"
