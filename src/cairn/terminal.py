"""Finding the tmux pane a process runs in, and typing one line into it.

This is the terminal half of the optional nudger (`docs/design.md` §5). It knows
nothing about messages, agents, or any agent product: it is handed a pid and one
line of text, and it puts that text into whatever pane that pid lives in. What
the line *says* is somebody else's problem — and per invariant I2 it is a bell,
never content.

Two facts, both measured here on 2026-08-01 against tmux 3.4, are the entire
reason this is a module and not three lines inline.

**The text and the `Enter` are two separate `send-keys` calls.** Against an agent
TUI with bracketed paste enabled, a single `tmux send-keys -t %1 'text' Enter`
leaves the text sitting unsubmitted in the input buffer — the Enter arrives
inside the paste and is taken as a newline. Sending the text, and then `Enter` as
a second invocation, submits it. Observed both ways, on the target this module
exists for.

Scope that claim honestly: a plain `bash` with `enable-bracketed-paste on`
submits either way, so a shell is not a reproduction of the failure. The split is
correct in both cases and strictly safer in one, which is why it stays — and why
`tests/test_terminal.py` asserts on the recorded argv, so nobody can collapse it
back after testing only against a shell and concluding it does not matter.

**pid to pane resolution walks the process-ancestor chain.**
`tmux list-panes -a -F '#{pane_id} #{pane_pid}'` reports each pane's *root*
process, and the process being looked for is nearly always a descendant of it —
a shell, then a CLI, then whatever that spawned. So walk the target pid's
ancestors and take the first one that is a pane root. Measured, for a process
three levels down: the chain was `[2771365, 2771364, 2770592, 2770591]` and the
pane pid was `2770592`. It resolved on the first try.

The walk has one parsing hazard worth stating in full. Field 2 of
`/proc/<pid>/stat` is the executable name wrapped in parentheses, and it may
itself contain spaces *and* parentheses, so `line.split()[3]` reads the wrong
field for a process named `(weird (proc) name)`. Split on the **last** `)` and
index from there; that is what `parse_ppid` does, and there is a test for it.

Everything that touches the world outside this process — the subprocess runner
and the ppid reader — is an injectable parameter with a real default, so the
whole module is testable without tmux installed and without reading `/proc`.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol

TMUX = "tmux"
"""The binary, resolved from PATH. Not configurable; if it moves, PATH is wrong."""

PANE_FORMAT = "#{pane_id} #{pane_pid}"
"""One pane per line, as `%<n> <pid>`. The pid is the pane's *root* process."""

TIMEOUT_SECONDS = 2.0
"""Every subprocess call is bounded. A tmux that has not answered by now is stuck,
and a nudger blocked on it is worse than a nudge that never arrives."""

PROC = Path("/proc")
"""Where `read_ppid` looks. A module-level name so a test could point it elsewhere."""


@dataclass(frozen=True, slots=True)
class Pane:
    """One tmux pane: its id, and the pid of the process at its root."""

    pane_id: str
    """tmux's own handle for the pane, e.g. `%1`. Stable while the pane lives."""

    pane_pid: int
    """The pane's root process. The pid you care about is usually a descendant."""


class TerminalUnavailable(RuntimeError):  # noqa: N818 - the name is a condition of the machine, not a defect
    """tmux is not installed, not running, or the pane is gone."""


class CommandResult(Protocol):
    """The part of `subprocess.CompletedProcess` this module actually reads."""

    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    """The shape of `subprocess.run` that this module depends on.

    Injected into every function that shells out, with `subprocess.run` itself as
    the default, so tests can record argv and hand back canned results without a
    tmux server anywhere. Implementations receive an argument *list* and must
    never involve a shell.
    """

    def __call__(
        self,
        args: Sequence[str],
        /,
        *,
        capture_output: bool = ...,
        text: bool = ...,
        timeout: float | None = ...,
        check: bool = ...,
    ) -> CommandResult:
        """Run `args` as a plain argument vector and return its result."""


type PpidReader = Callable[[int], int | None]
"""Maps a pid to its parent pid, or `None` if the process is gone or unreadable."""


def parse_ppid(stat_line: str) -> int | None:
    """Return the parent pid from the contents of a `/proc/<pid>/stat` file.

    Field 2 is the executable name in parentheses and may contain spaces and
    parentheses of its own, so this splits on the **last** `)` and counts from
    there: state, then ppid. Never index the whole line — `1234 (weird (proc)
    name) S 42 ...` has a ppid of 42, and `stat_line.split()[3]` says `name)`.

    Returns `None` for anything that does not parse, since an unreadable stat
    file is a process that ended, not a bug worth raising over.
    """
    close = stat_line.rfind(")")
    if close == -1:
        return None
    after_comm = stat_line[close + 1 :].split()
    ppid_field = 1  # after the comm come: state, ppid, ...
    if len(after_comm) <= ppid_field:
        return None
    try:
        return int(after_comm[ppid_field])
    except ValueError:
        return None


def read_ppid(pid: int) -> int | None:
    """Return the parent of `pid` according to `/proc`, or `None`.

    The default `PpidReader`. Every failure — no such process, no `/proc` on this
    kernel, a stat file that changed shape — is `None`, because the caller's only
    sane response to any of them is to stop walking.
    """
    try:
        stat_line = (PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return parse_ppid(stat_line)


def _fail(args: Sequence[str], result: CommandResult) -> NoReturn:
    """Raise `TerminalUnavailable` naming the command, its status and its stderr."""
    detail = (result.stderr or "").strip()
    suffix = f": {detail}" if detail else ""
    msg = f"`{shlex.join(args)}` exited {result.returncode}{suffix}"
    raise TerminalUnavailable(msg)


def _tmux(run: Runner, args: Sequence[str]) -> CommandResult:
    """Run one tmux command, or raise `TerminalUnavailable` explaining why not.

    An argument list, never a shell string: the text being typed is arbitrary and
    a shell would be one quoting mistake away from executing it here instead of
    delivering it there.
    """
    argv = [TMUX, *args]
    try:
        result = run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as exc:
        msg = f"tmux is not installed or not on PATH ({exc})"
        raise TerminalUnavailable(msg) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"`{shlex.join(argv)}` did not complete: {exc!r}"
        raise TerminalUnavailable(msg) from exc
    if result.returncode != 0:
        _fail(argv, result)
    return result


def tmux_available(run: Runner = subprocess.run) -> bool:
    """Report whether tmux is installed *and* a server is currently running.

    `tmux has-session` answers both at once: it exits 0 with a live server, exits
    1 with `error connecting to /tmp/tmux-<uid>/default` when there is none, and
    raises `FileNotFoundError` when the binary is absent. Callers use this to
    decide whether nudging is possible at all, so it reports rather than raises.
    """
    try:
        _tmux(run, ["has-session"])
    except TerminalUnavailable:
        return False
    return True


def list_panes(run: Runner = subprocess.run) -> list[Pane]:
    """Return every pane on this machine's tmux server, across all sessions.

    Lines that do not parse are skipped rather than fatal: a future tmux that
    adds a field should cost the nudger the panes it cannot read, not every pane.
    An empty list means the server reported no panes.
    """
    result = _tmux(run, ["list-panes", "-a", "-F", PANE_FORMAT])
    panes: list[Pane] = []
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        expected = 2
        if len(fields) != expected:
            continue
        pane_id, pane_pid = fields
        try:
            panes.append(Pane(pane_id=pane_id, pane_pid=int(pane_pid)))
        except ValueError:
            continue
    return panes


def ancestors(pid: int, ppid_of: PpidReader = read_ppid) -> list[int]:
    """Return `[pid, parent, grandparent, ...]` up to init. Never loops, never raises.

    Termination is by construction: a pid already in the chain is never followed a
    second time, so a `/proc` that reports a cycle — or a fake reader in a test
    that does — ends the walk instead of hanging it. A reader that raises is
    treated as a reader that returned `None`, because this runs inside a nudger
    whose worst acceptable failure is a nudge that does not arrive.
    """
    if pid < 1:
        return []
    chain: list[int] = []
    seen: set[int] = set()
    current: int | None = pid
    while current is not None and current > 0 and current not in seen:
        seen.add(current)
        chain.append(current)
        try:
            current = ppid_of(current)
        except Exception:  # noqa: BLE001 - a broken reader must end the walk, not the nudger
            break
    return chain


def pane_for_pid(pid: int, run: Runner = subprocess.run, ppid_of: PpidReader = read_ppid) -> Pane | None:
    """Return the pane `pid` is running in, or `None` if it is not in one.

    `None` is a normal answer, not an error: plenty of sessions run outside tmux
    and they simply cannot be nudged (`docs/design.md` §5 says to state that
    plainly rather than work around it). A tmux that is installed but broken is a
    different thing and still raises `TerminalUnavailable` from `list_panes`;
    call `tmux_available` first if you would rather not distinguish them.
    """
    roots: dict[int, Pane] = {}
    for pane in list_panes(run):
        roots.setdefault(pane.pane_pid, pane)  # first pane wins, so the answer is stable
    if not roots:
        return None
    for ancestor in ancestors(pid, ppid_of):
        pane = roots.get(ancestor)
        if pane is not None:
            return pane
    return None


def send_line(pane_id: str, text: str, run: Runner = subprocess.run) -> None:
    """Type `text` into the pane and submit it — as two separate send-keys calls.

    The split is the measured fact in this module's docstring: with bracketed
    paste on, one combined call leaves the text unsubmitted in the buffer. Do not
    simplify it.

    `-l` sends the text literally, so a line that happens to read `Up` or `C-c`
    arrives as those characters instead of as that keypress; `--` keeps a leading
    dash out of tmux's option parsing. The second call deliberately omits `-l`,
    because there `Enter` *is* the key name.

    Raises `ValueError` for text containing a newline or a carriage return. A
    nudge is one line, and half a line submitted into someone's live prompt is
    how a stray fragment becomes a command they did not type.
    """
    if "\n" in text or "\r" in text:
        msg = f"a nudge is a single line; refusing text containing a newline or carriage return: {text!r}"
        raise ValueError(msg)
    _tmux(run, ["send-keys", "-t", pane_id, "-l", "--", text])
    _tmux(run, ["send-keys", "-t", pane_id, "Enter"])
