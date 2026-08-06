"""The key on this machine, and exactly which bytes it covers.

This is the local half of docs/design.md §12 item 9, and it is deliberately only
the local half. `provenance.assess_sent` argued the way in before any of this
existed: verifying your **own** send is the one check that can succeed before any
key exchange exists at all, because both halves are on this machine. So there is
no key distribution here, no hub to trust, and no dependency — `hmac`, `hashlib`
and `secrets` are stdlib, which matters because cairn's `dependencies = []` is a
decision rather than an accident and the stdlib has no asymmetric primitive to
offer. Peer verification needs one, and stays open.

What it detects is a hub putting words in your mouth. That is the worse of the
two lies a hub can tell: a hub lying to `cairn inbox` is a stranger speaking as
your peer, which a reader has some instinct for, while a hub lying to
`cairn sent` reads as your own memory and so gets weighed less rather than more.

**The key is per working directory**, keyed by the same `config._slug` scheme as
identity and pins, and that is a design choice with a visible consequence. A name
taken over from another directory is signed by a key this directory does not
have, so its predecessor's sends will not verify here — which is the honest
answer, since they are not this session's words and it cannot prove they were.
That case is also where the first genuinely mixed reading comes from, and
`render.sent_text` is built to announce one.

Two sessions sharing a directory under different `CAIRN_AGENT` names share a key.
That is fine and is not the threat: the question this answers is whether the
*hub's* record matches what this machine sent, not which of two sibling sessions
on one box typed it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import TYPE_CHECKING

from cairn.config import key_file

if TYPE_CHECKING:
    from pathlib import Path

    from cairn.wire import Message

METHOD = "hmac-sha256"
"""What `Provenance.token()` prints when a signature checks out.

Named rather than left as "verified", because the method is the only part of the
verdict that says how much was proven, and this one proves less than a reader
might assume. See `canonical`.
"""

SCHEMES = frozenset({METHOD})
"""The signature schemes this build knows how to check.

**Nothing in this build writes a scheme prefix, and that asymmetry is the entire
point of the three functions below.** They read a syntax `sign` does not produce,
which reads as dead code and will keep reading that way for as long as there is
one scheme — possibly years. Deleting them costs nothing today and everything on
the day somebody adds the second.

The reason is what happens without them. `verify` compares against a key, so a
signature made under a scheme this build cannot compute simply fails to match,
and `provenance.assess_sent` had exactly two branches for that: matched, or
`MISMATCH`. `MISMATCH` is the loudest verdict cairn has and `Provenance.mismatch`
says it means *a check ran and failed*. So the first build to emit a new scheme
would turn every reading on every not-yet-upgraded peer into that verdict, across
the whole history, for no reason but a version skew — the largest false alarm
this product is capable of producing, delivered by an ordinary upgrade.

Tolerance therefore has to ship **before** anything emits, which is why this half
lands alone. `sign` keeps returning bare hex, `scheme_of` reads bare hex as
`METHOD`, and every row already on every hub keeps verifying exactly as it did.
When a second scheme is eventually added, the build that adds it can emit a
prefix knowing that every peer old enough to have this code reports `UNVERIFIED`
— *nothing was checked* — rather than accusing them of forgery.

Whoever adds that scheme: add it here, and read `canonical` first. The bytes are
pinned by a test and changing them is a separate decision from changing the
algorithm over them.
"""


def scheme_of(signature: str) -> str:
    """Return the scheme `signature` declares.

    The wire form is `<scheme>:<hex>`, and a signature with no colon is the
    original scheme rather than a malformed one — every signature written before
    this function existed is bare hex, and they are the only signatures that
    exist. Reading them as `METHOD` is what keeps this change invisible.

    The return value comes off the wire, so it is a string a peer chose. Do not
    interpolate it into anything printed: see `provenance.assess_sent`, which
    deliberately does not name it, and docs/design.md §12 item 23 for the
    precedent.
    """
    scheme, separator, _ = signature.partition(":")
    return scheme if separator else METHOD


def digest_of(signature: str) -> str:
    """Return the hex half of `signature`, with any scheme prefix removed.

    Tolerant in the direction that matters: a future build emitting
    `hmac-sha256:<hex>` for the scheme this one already implements is verified
    here correctly, because the prefix is stripped and the remaining bytes are
    what `sign` produces. Only a genuinely unknown scheme is unreadable.
    """
    _, separator, digest = signature.partition(":")
    return digest if separator else signature


def can_check(signature: str) -> bool:
    """Return whether this build implements the scheme `signature` was made under.

    Callers must ask this **before** treating a failed `verify` as evidence of
    anything; `verify`'s docstring says why, and `provenance.assess_sent` is the
    worked example.
    """
    return scheme_of(signature) in SCHEMES


_KEY_BYTES = 32


def key(cwd: Path | None = None) -> bytes:
    """Return this directory's signing key, creating one on first use.

    Created rather than required, because a key that has to be set up is a key
    most machines will not have, and the whole value of this cut is that the
    verdict starts varying on machines nobody visited. `secrets.token_bytes` and
    mode `0600`: the file is a secret in the ordinary sense, and it is the only
    one cairn has ever written.

    A lost key is not a failure mode worth guarding. It costs the ability to
    verify sends made before it went, which reports as `UNVERIFIED` — the answer
    this whole surface printed until this cut, and an honest one.
    """
    path = key_file(cwd)
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        return bytes.fromhex(str(raw["key"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = secrets.token_bytes(_KEY_BYTES)
    path.write_text(json.dumps({"key": fresh.hex()}), encoding="utf-8")
    path.chmod(0o600)
    return fresh


def canonical(message: Message) -> bytes:
    """Return the bytes a signature covers — which is not the whole message.

    **`seq` and `created_at` are outside it, and that is not an oversight.** The
    hub assigns both (`store.append` calls `now()` itself and takes the sequence
    from the insert), so a sender has nothing to sign them with. What a signature
    therefore proves is the words, the addressee and the kind. It does not prove
    when the hub says you sent them, or in what order relative to anything else.

    That limit belongs here rather than in a footnote because the surface this
    feeds exists to stop cairn claiming more than it checked. A hub that
    re-dated or re-ordered your sends would still hand back rows that verify.

    **The field list is written out rather than derived from `Message`.** A
    field added to the schema later would otherwise fall silently inside or
    outside the signature depending on how the derivation happened to work, and
    which of those it should be is a decision somebody has to make. Making this
    function fail to compile the question is the point of spelling it out.

    **Changing what is covered *is* changing the scheme, and the two have to move
    together.** These bytes are not a private detail: every signature on every
    hub was made over the output of this function as it stood that day, and
    nothing on the wire records which day that was. Add a field, drop one, or
    change the serialization, and every stored row stops matching — and it stops
    matching as `MISMATCH`, the verdict that means *a check ran and failed*,
    rather than as anything a reader could correctly ignore. That is a one-way
    door if the scheme name stays put, and an ordinary additive change if it does
    not, which is what `SCHEMES` exists to make possible. So: a new field list
    ships under a new entry in `SCHEMES`, or it does not ship.

    `test_the_bytes_a_signature_covers_are_pinned` holds the literal output for
    one fully-populated message so that a change here cannot be a quiet one. It
    will look like a brittle test. It is a tripwire, and the thing on the other
    side of it is every signature anybody has already stored.

    Note what that pin transitively covers: `Artifact.to_json` is called here, so
    a field added to `Artifact` also changes these bytes. That is correct — it is
    inside the signature — and it is the change least likely to be noticed from
    this file.

    Serialized through `json.dumps` with sorted keys and no spaces, so the bytes
    do not depend on dict ordering or on anybody's formatter.

    Taking a whole `Message` and then covering six of its nine fields is on
    purpose. The alternative — six parameters — is the same list written where a
    caller has to keep it in order, and `client.send` already builds exactly
    these six as its POST body, which is the evidence that this is the seam
    rather than an arbitrary subset.
    """
    payload = {
        "kind": message.kind,
        "sender": message.sender,
        "recipient": message.recipient,
        "body": message.body,
        "correlation_id": message.correlation_id,
        "artifacts": [a.to_json() for a in message.artifacts],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(message: Message, cwd: Path | None = None) -> str:
    """Return a hex HMAC over `canonical`, using this directory's key.

    **Bare hex, with no `METHOD:` prefix, and that is deliberate rather than an
    oversight `scheme_of` was written to paper over.** Emitting a prefix would
    make every not-yet-upgraded peer read this build's sends as `MISMATCH`; see
    `SCHEMES` for why that is the worst thing an upgrade can do here. The prefix
    is a syntax this build reads and does not write, and it stays that way until
    the build that introduces a second scheme.
    """
    return hmac.new(key(cwd), canonical(message), hashlib.sha256).hexdigest()


def verify(message: Message, cwd: Path | None = None) -> bool:
    """Return whether `message.signature` is one this directory's key would have produced.

    **`False` here means one of three things and the caller has to separate
    them**, because they are three different findings and only one of them is
    evidence against anybody:

    - *nobody signed this* — an empty signature. `provenance.assess_sent` checks
      for that before calling.
    - *this build cannot check that scheme* — `can_check` is false. Also checked
      before calling, and for the same reason: a scheme this build has never
      heard of produces bytes it cannot compute, so the mismatch here says
      nothing about the sender.
    - *this signature is wrong* — the only one that is evidence of something.

    Flattening any of them into one `False` is the defect this whole surface
    exists to avoid, and the second was added by the cut that made a scheme
    prefix readable. It is written as a caller obligation rather than a guard in
    here on purpose: a guard would have to pick a return value, and every value
    it could pick is one of the three findings above wearing another's clothes.

    `hmac.compare_digest` rather than `==`, on the ordinary timing-attack
    grounds. It is close to pointless here — the attacker would need to be
    running on the machine that holds the key — and it is one word, so the
    reason to write it is that the version without it is the one that gets
    copied somewhere it matters.
    """
    return hmac.compare_digest(sign(message, cwd), digest_of(message.signature))
