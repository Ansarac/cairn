"""Exceptions that carry their exit code.

`cli.run()` turns a `CairnError` into an exit code and a one-line message, and
lets anything else keep its traceback. Do not widen that catch — a stack trace
from a real bug is more useful than a tidy error message that hides it.

The codes are meant to be distinguishable by a script, and two of them mean
opposite things:

    1  asked, and the answer is "nothing"   -- an inbox with no mail
    2  could not ask                        -- the hub is unreachable
    4  not allowed to ask                   -- the hub refused this machine's token

Collapsing those two loses the only fact that matters when a peer goes quiet:
whether it said nothing, or whether nobody heard the question.

`4` is kept out of `2` on the same reasoning one step along. Both are failures
to get an answer, and a script's correct response to each is the opposite: `2`
is a hub that may be back in a minute, so retry; `4` is a credential this
machine will keep getting wrong forever, so stop and fetch a human. Folded
together, a token typo reads as an outage and somebody spends an evening on the
network.
"""

from __future__ import annotations


class CairnError(Exception):
    """Base class for errors that should exit cleanly rather than traceback."""

    exit_code = 2


class Unreachable(CairnError):  # noqa: N818 - the name is the condition; `UnreachableError` reads as a broken build
    """The hub could not be reached, or refused the request."""

    exit_code = 2


class Unreadable(Unreachable):
    """The hub answered, and this build could not parse what it said.

    **A subclass rather than a sibling, so the exit code cannot drift.** To a
    script these are one situation — the hub is not usable from here, exit 2 —
    and splitting that would be the collapse `errors.py` argues against
    everywhere else, in reverse. Every `except Unreachable` keeps catching this.

    What the distinction buys is one caller. `cli.cmd_bell` must stay silent
    through an ordinary outage, because it runs at every turn boundary and a
    line printed on every one of them is furniture (docs/design.md §12 item 18).
    It must *not* stay silent when a page cannot be parsed, because that is a
    poisoned mailbox reporting itself as an empty one — the failure §12 item 26
    is about, which reached the bell as an `Unreachable` indistinguishable by
    type from a hub that was merely down.

    Nothing else should branch on this. If a second caller wants it, ask first
    whether it actually wants "the hub is not usable", which is the parent.
    """


class NoRoute(Unreachable):
    """The hub answered, and does not have the path this command needs.

    A subclass for the reason `Unreadable` is one: to a script this is still
    "the hub is not usable for this", exit 2, and every `except Unreachable`
    keeps catching it. Splitting the exit code would be the collapse this module
    argues against, in reverse.

    What it buys is a command that can say *why*. A hub built before a route
    exists answers 404, which `client._call` maps into "hub unreachable" — and
    that sentence sends whoever reads it to look at the network, when the hub is
    up, healthy, and carrying traffic. Measured on the fleet this was written for:
    the production hub predates `cairn rename` by one release, so this is the
    first thing an operator would have hit.

    Only a command whose own route is new should branch on this. Anything older
    catching it would be claiming a hub is out of date on the evidence of a
    typo'd path.
    """


class Unauthorized(CairnError):  # noqa: N818 - a state of this machine's configuration, not a defect
    """The hub is up, and it will not talk to this machine.

    Distinct from `Unreachable` because the two want opposite responses from
    whoever hits them; the module docstring has the argument. The message is
    written here rather than taken from the hub's reply: the hub has no idea
    where this machine keeps its configuration, and it is the only party that
    does not need telling what went wrong.
    """

    exit_code = 4

    def __init__(self, where: str) -> None:
        """Name the hub that refused, and the two places a token may be set."""
        super().__init__(
            f"the hub at {where} requires a token and did not accept this machine's. "
            f"Set CAIRN_TOKEN in the environment, or `token` in the config file "
            f"(`cairn config` prints its path). Retrying will not help."
        )


class UsageError(CairnError):
    """The command was well-formed but cannot be carried out as asked."""

    exit_code = 3


class NotRegistered(CairnError):  # noqa: N818 - ditto: this is a state of the session, not a defect
    """Raised when this session has not joined the network yet."""

    exit_code = 3

    def __init__(self, detail: str = "") -> None:
        """Build the standard message, appending `detail` in parentheses if given."""
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"this session is not registered{suffix}; run `cairn register <name>` first")


class NameMoved(CairnError):  # noqa: N818 - a fact about the network, not a fault in the caller
    """A name no longer reaches what it reached earlier from this directory.

    Nothing is sent. Failing closed is the point: the alternative is delivering
    a message meant for a colleague to whoever happens to hold the name now, and
    the sender never learning that it happened.
    """

    exit_code = 3

    def __init__(self, name: str, was: str, now_is: str) -> None:
        """Name what changed, and what the name used to reach."""
        super().__init__(
            f"{name!r} now reaches {now_is}, but earlier sends from this directory went to {was}. "
            f"Nothing was sent. If the move is expected, run `cairn forget {name}` and send again."
        )
