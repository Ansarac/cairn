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

Signing has landed for one of the three, and the split this module was already
carved into is why that was possible: `assess_sent` verifies, `assess` and
`assess_note` still do not. The three were separate functions before there was
anything to put in them, on the argument that they would not check the same bytes
— and the first cut of docs/design.md §12 item 9 is exactly that argument coming
true, since your own signature is checkable with no key exchange and a peer's is
not. Do not collapse them now that one of them does something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cairn import signing
from cairn.wire import Provenance

if TYPE_CHECKING:
    from cairn.wire import Message, Note


def assess(message: Message) -> Provenance:
    """Return what this build actually verified about `message`.

    Still nothing, and the verdict does not move in this cut. A peer signs with a
    key on the peer's machine; checking it needs a key exchange that does not
    exist, so `UNVERIFIED` is the honest answer and it is deliberately loud.

    **What did change is that a peer's message can now arrive carrying a
    `signature`, and this function is what stops that reading as reassurance.**
    That is the exact failure this module was built from: an agent shown a
    `verified_by: "cairn-hub"` field refused to act on it, correctly, because
    nothing verified it. A cryptographic-looking field nobody on this machine can
    check is the same shape wearing better clothes — and the first cut of
    docs/design.md §12 item 9 is what put it on the wire, so the sentence that
    forecloses the misreading belongs to the same cut.

    So the detail names it, and only when there is one. The common row is
    unchanged, because a clause printed on every reading is a clause already
    reported three times as furniture; a clause that appears exactly when a new
    kind of thing is present has some chance of being read.
    """
    unchecked = "hub does not sign yet; sender identity is asserted, not proven"
    if message.signature:
        return Provenance.unverified(
            f"{unchecked}. This row carries a signature, and it is the sender's own — "
            "verifying it needs their key, which this machine does not have"
        )
    return Provenance.unverified(unchecked)


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


def assess_sent(message: Message) -> Provenance:
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

    **This is the one that now answers something, and it answers three ways.**
    The prediction above held: the easiest win was the local one, and it is the
    only function here that changes in this cut. `assess` and `assess_note` still
    return `unverified` because peer verification needs a key this machine does
    not have.

    The three-way split is the part to keep. An absent signature is evidence of
    nothing — that row predates the build, or crossed a hub too old to store one.
    A signature that *fails* is evidence of something, and giving both the same
    word would hand the loudest row on the page the vocabulary of the most
    ordinary one. The failure is still usually benign, which is why the cause is
    named in the detail rather than left to be imagined: the key is per working
    directory, so a name taken over from somewhere else is signed by a key this
    directory does not hold.

    **What a pass claims is narrower than it looks**, and the detail says so on
    every reading that has one. The hub assigns `seq` and `created_at`, so
    neither is inside the signature: a hub that re-dated or re-ordered your sends
    would hand back rows that verify. `signing.canonical` is where that list is
    fixed.
    """
    if not message.signature:
        return Provenance.unverified(
            "no signature on this one — sent before this build, or through a hub too old to carry it, "
            "so it is the hub's record of your send and not proof of it"
        )
    if signing.verify(message):
        return Provenance(
            verified=True,
            method=signing.METHOD,
            detail="checked against this directory's key; it covers the words, the addressee and the kind, "
            "and not the sequence or the time, which the hub assigns",
        )
    return Provenance.mismatch(
        signing.METHOD,
        "a signature is present and this directory's key does not reproduce it. The ordinary cause is a name "
        "taken over from another working directory, whose sends this machine cannot verify and should not",
    )
