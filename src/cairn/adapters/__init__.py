"""Everything that knows about a specific agent product.

The rest of `src/cairn/` must not name a vendor — not in an import, not in a
path, not in a string. `just guard` enforces that with a grep, and CI runs it.

The rule is not tidiness. cairn's whole claim is that it works with any agent
that can run a shell command, and the only way that claim stays true over years
is if there is exactly one place where it could go wrong. When a second product
is supported, it becomes a second file here and nothing above changes.

What legitimately belongs in an adapter:

- where that product installs skills
- how to register a turn-boundary hook with it
- how to read its local session state, if it publishes any

What does not: message shapes, delivery rules, storage, or anything a second
product would also need.
"""

from __future__ import annotations

from types import ModuleType

from cairn.adapters import claude_code


def default() -> ModuleType:
    """Return the adapter for the agent product hosting this session.

    Core reaches a product only through this call. Writing a module name in
    `cli.py` would put a vendor name outside this package, which is the single
    thing the guard forbids — so the indirection is the boundary, not
    decoration.

    One product is supported today, so there is nothing to detect and this
    returns a constant. When a second arrives, the detection goes here and
    nothing above `adapters/` changes.
    """
    return claude_code
