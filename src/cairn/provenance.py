"""How much a received message can be trusted, decided locally.

This module exists so that "we have not built signing yet" is a visible,
testable fact rather than a silence.

The reason is a measurement. An agent was shown a peer message carrying a
`verified_by: "cairn-hub"` field and refused to act on it, correctly:

    verified_by is just a string in the JSON payload — nothing actually
    verifies it. Anyone who can write to this directory can drop a message
    here claiming to be verified.

It was right, and the lesson generalises past that one field. A trust claim is
worth exactly the check that produced it. So the only thing allowed to build a
`Provenance` is the code that ran a check, and with no scheme configured the
answer is `unverified` with a reason — never a comforting default.

When signing lands, this is the one function that changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cairn.wire import Provenance

if TYPE_CHECKING:
    from cairn.wire import Message


def assess(message: Message) -> Provenance:  # noqa: ARG001 - the signature is the seam
    """Return what this build actually verified about `message`.

    Today: nothing. There is no signing scheme yet, so every message comes back
    `unverified`, and `cairn inbox` says so in its output. That is the honest
    answer and it is deliberately loud — a reader who sees `UNVERIFIED` on every
    message will treat peer content as a claim, which is what invariant I1 asks
    for anyway.
    """
    return Provenance.unverified("hub does not sign yet; sender identity is asserted, not proven")
