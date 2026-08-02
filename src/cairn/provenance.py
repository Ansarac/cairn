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
    from cairn.wire import Message, Note


def assess(message: Message) -> Provenance:  # noqa: ARG001 - the signature is the seam
    """Return what this build actually verified about `message`.

    Today: nothing. There is no signing scheme yet, so every message comes back
    `unverified`, and `cairn inbox` says so in its output. That is the honest
    answer and it is deliberately loud — a reader who sees `UNVERIFIED` on every
    message will treat peer content as a claim, which is what invariant I1 asks
    for anyway.
    """
    return Provenance.unverified("hub does not sign yet; sender identity is asserted, not proven")


def assess_note(note: Note) -> Provenance:  # noqa: ARG001 - the signature is the seam
    """Return what this build actually verified about `note`.

    A second function rather than a widened `assess`, because when signing lands
    these two will not check the same bytes: a message is signed once by its
    sender at the moment it is sent, while a note is read by somebody who was not
    there and may be verifying an author who has since left the network. Sharing
    one function would hide that difference behind an `isinstance`.

    Today the answer is the same and it is the honest one — nothing was checked
    — with the wording adjusted for the thing that actually matters when reading
    sediment: age. An old note is not wrong because it is old, but nothing has
    re-checked it either.
    """
    return Provenance.unverified("hub does not sign yet; the author name is asserted, not proven")


def assess_sent(message: Message) -> Provenance:  # noqa: ARG001 - the signature is the seam
    """Return what this build actually verified about a record of your own send.

    A third function on exactly the reasoning that made `assess_note` a second:
    when signing lands, these will not check the same bytes. Verifying a peer's
    message means checking a key you do not hold. Verifying your **own** send
    means checking a signature you made — the one check that can succeed before
    any key exchange exists at all, because both halves are on this machine.
    Sharing one function would hide that behind an `isinstance` and would make
    the easiest win look like the hardest.

    The wording differs from `assess` for a reason worth stating. On the inbox,
    `UNVERIFIED` qualifies **who sent this**. Here the sender is not in doubt —
    it is you — and what is unproven is whether these are the words you sent, or
    even that you sent anything. A hub that lies to `cairn inbox` is a stranger
    putting words in a peer's mouth, and a reader has some instinct for that. A
    hub that lies here is putting words in *your* mouth, which reads as memory
    rather than as testimony and so gets weighed less, not more.
    """
    return Provenance.unverified("hub does not sign yet; this is the hub's record of your send, not proof of it")
