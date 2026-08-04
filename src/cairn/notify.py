"""Handing the bell to a person, by running a command the operator configured.

The successor to the withdrawn nudger, and deliberately a much smaller thing.
`docs/design.md` §5 has why the daemon went; the short version is that it was the
only component in cairn able to begin an unsupervised chain of agent action from
zero, because it manufactured turn boundaries out of nothing. This module changes
the target from the agent to the person. A chat application notifies a human, who
then decides; that puts a human in the loop structurally rather than by
discipline, which is a better fit for I2 than the thing it replaces.

**cairn emits and stops.** It ships nothing that receives this, names no service,
and needs no pid, no multiplexer and no status field — which is the whole of what
made its predecessor fragile. The hazard to hold on to: the property wanted here
is *a human decides*, and that comes from the notification reaching a person, not
from which transport carries it. **A bridge that automatically relays a cairn bell
back into a session is the nudger again**, with a third party added to the path
and worse latency. If somebody builds one, it is not cairn's and does not belong
here.

What makes routing this through a channel cairn does not trust safe at all is I1,
arriving from an unexpected direction: the bell may not carry content, so what
leaves the network is a count and a name and never a message body. That was
written to stop unattributable text reaching a model through a hook. It pays a
second time here.

Two shapes, and the difference matters:

- `fire` is the hook path. It **detaches and does not wait**, so a turn boundary
  is never slowed and a notification crossing a slow network is never cut off
  half-sent. It cannot raise, because `cli.cmd_bell` must never fail loudly.
- `probe` is the operator path behind `cairn bell --test`. It **waits**, captures
  and reports, because the cost of `fire`'s silence is that a misconfigured
  command is otherwise indistinguishable from a quiet week.

Imports nothing local, and every call into the outside world is an injectable
parameter with a real default, so the tests spawn nothing.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

COUNT = "{count}"
AGENT = "{agent}"
REASON = "{reason}"
PLACEHOLDERS = (COUNT, AGENT, REASON)
"""What an operator may write in an argv element, substituted per slot.

Deliberately not `str.format`. Two reasons, both of which have bitten this shape
elsewhere: a literal `{` is ordinary in a notification payload — `["curl", "-d",
'{"topic":"cairn"}']` is a realistic line — and `format` raises on it; and
`format` reaches attributes, so `{count.__class__.__mro__}` is a template the
operator did not think they were writing. Three `str.replace` calls have neither
property.
"""

ENV_PREFIX = "CAIRN_BELL_"
"""The same three values as environment variables, for a command that is a script.

A placeholder only helps a command that takes the value as an argument.
`notify-send` does; a shell script does not, and neither does anything that wants
to build its own payload. Both routes carry exactly the same three values.
"""

PROBE_TIMEOUT_SECONDS = 10.0
"""How long `cairn bell --test` waits before calling it stuck.

Generous where `terminal.TIMEOUT_SECONDS` is tight, and for the opposite reason:
nothing is blocked on this but an operator who is sitting there watching, and a
notification that reaches a phone service in eight seconds is working.
"""


class Spawner(Protocol):
    """The shape of `subprocess.Popen` that `fire` depends on.

    Injected so a test can record what would have been spawned without spawning
    it. Implementations receive an argument *list* and must never involve a
    shell — the same rule `terminal.py` keeps, for a weaker reason here (the
    operator wrote this line themselves) but with the same payoff: `["sh", "-c",
    …]` still works and is then a choice somebody can see in the config file.
    """

    def __call__(  # noqa: PLR0913 - mirrors `subprocess.Popen`; the count is stdlib's, not a design choice
        self,
        args: Sequence[str],
        /,
        *,
        stdin: int = ...,
        stdout: int = ...,
        stderr: int = ...,
        env: Mapping[str, str] | None = ...,
        start_new_session: bool = ...,
    ) -> object:
        """Start `args` as a plain argument vector and return without waiting."""


class Waiter(Protocol):
    """The shape of `subprocess.run` that `probe` depends on."""

    def __call__(  # noqa: PLR0913 - mirrors `subprocess.run`; the count is stdlib's, not a design choice
        self,
        args: Sequence[str],
        /,
        *,
        capture_output: bool = ...,
        text: bool = ...,
        timeout: float = ...,
        check: bool = ...,
        env: Mapping[str, str] | None = ...,
    ) -> subprocess.CompletedProcess[str]:
        """Run `args` as a plain argument vector and return its completed result."""


@dataclass(frozen=True, slots=True)
class Probe:
    """What `cairn bell --test` found out, for `render` to say.

    `returncode` is `None` when the command never produced one — it could not be
    started, or it was still running when the timeout ran out. Those are
    different failures from "it ran and exited 1", and an operator reading the
    report needs to tell them apart, so `detail` says which.
    """

    argv: tuple[str, ...]
    returncode: int | None
    detail: str

    @property
    def ok(self) -> bool:
        """True only when the command ran to completion and exited 0."""
        return self.returncode == 0


def build_argv(template: Sequence[str], count: int, agent: str, reason: str) -> list[str]:
    """Substitute the three placeholders into each argv element, per slot.

    Per slot is the safety property. A value lands inside one element and is
    never re-parsed, so there is no quoting to get wrong and nothing an unusual
    agent name can do to the shape of the command. Compare the string-plus-shell
    form this deliberately is not.
    """
    values = {COUNT: str(count), AGENT: agent, REASON: reason}
    out = []
    for element in template:
        rendered = element
        for token, value in values.items():
            rendered = rendered.replace(token, value)
        out.append(rendered)
    return out


def bell_env(count: int, agent: str, reason: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the inherited environment plus the three `CAIRN_BELL_*` values."""
    env = dict(os.environ if environ is None else environ)
    env[f"{ENV_PREFIX}COUNT"] = str(count)
    env[f"{ENV_PREFIX}AGENT"] = agent
    env[f"{ENV_PREFIX}REASON"] = reason
    return env


def fire(argv: Sequence[str], env: Mapping[str, str], spawn: Spawner = subprocess.Popen) -> bool:
    """Start the operator's command and return immediately. Never raises.

    Three of the arguments below are load-bearing and none is hygiene.

    **`stdout` and `stderr` go to `DEVNULL`.** `cli.cmd_bell`'s stdout *is* the
    hook payload the host parses. A child inheriting that fd can write a second
    line into it, at which point the host is parsing a notification tool's chatter
    as cairn's response. There is a test asserting both are closed.

    **`stdin` goes to `DEVNULL`** so a command that reads stdin cannot sit on the
    hook's, which the host may still be holding open.

    **`start_new_session=True`** puts the child in its own process group, so a
    host that tidies up after a hook by killing the group does not kill a
    notification that is still in flight. Without this the detached path would
    work under a test and fail exactly where it matters.

    Returns whether the spawn was attempted successfully, for a caller that wants
    to know. `cmd_bell` does not; it has nowhere to say it.
    """
    try:
        spawn(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def probe(argv: Sequence[str], env: Mapping[str, str], run: Waiter = subprocess.run) -> Probe:
    """Run the operator's command in the foreground and report what happened.

    **This proves the command, not the spawn mode**, and the difference is worth
    knowing before trusting a green result. Here the command inherits a terminal
    and its output is captured; on the real path it is detached with all three
    standard streams closed. A command that needs a tty, or that writes its error
    to stderr and exits 0, passes here and does nothing there.
    """
    frozen = tuple(argv)
    try:
        result = run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            env=dict(env),
        )
    except subprocess.TimeoutExpired:
        return Probe(frozen, None, f"still running after {PROBE_TIMEOUT_SECONDS:g}s, so it was killed")
    except FileNotFoundError as exc:
        return Probe(frozen, None, f"could not be started: {exc.strerror or exc}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Probe(frozen, None, f"could not be started: {exc!r}")
    return Probe(frozen, result.returncode, (result.stderr or "").strip())
