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
    """Return a hex HMAC over `canonical`, using this directory's key."""
    return hmac.new(key(cwd), canonical(message), hashlib.sha256).hexdigest()


def verify(message: Message, cwd: Path | None = None) -> bool:
    """Return whether `message.signature` is one this directory's key would have produced.

    A message with no signature is **not** a failure here and the caller has to
    tell the two apart: `provenance.assess_sent` checks for an empty signature
    before calling, because "nobody signed this" and "this signature is wrong"
    are different findings and flattening them into one `False` is the defect
    this cut exists to avoid on the surface above.

    `hmac.compare_digest` rather than `==`, on the ordinary timing-attack
    grounds. It is close to pointless here — the attacker would need to be
    running on the machine that holds the key — and it is one word, so the
    reason to write it is that the version without it is the one that gets
    copied somewhere it matters.
    """
    return hmac.compare_digest(sign(message, cwd), message.signature)
