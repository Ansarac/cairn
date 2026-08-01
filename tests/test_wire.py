"""The contract, checked directly.

The first test here is the important one: it asserts an *absence*. `Message`
must never grow a field that lets a sender vouch for itself. If someone adds
`verified` to `Message` for convenience, this test is what stops it.
"""

from __future__ import annotations

import dataclasses

import pytest

from cairn.wire import (
    MAX_BODY_CHARS,
    PROTOCOL_VERSION,
    Agent,
    Artifact,
    InboxEntry,
    Message,
    Provenance,
    WireError,
    check_version,
    dumps,
    loads,
)


def test_a_sender_cannot_claim_its_own_message_is_verified():
    """Invariant I1, expressed as a type. See provenance.py for why."""
    fields = {f.name for f in dataclasses.fields(Message)}
    forbidden = {"verified", "verified_by", "trusted", "signature_ok", "origin_is_human"}
    assert not (fields & forbidden), (
        f"Message grew a self-asserted trust field: {fields & forbidden}. "
        "Trust is Provenance, produced by whoever ran the check."
    )


def test_provenance_is_not_on_the_wire():
    """`InboxEntry` renders provenance for output; nothing parses it back in."""
    assert not hasattr(InboxEntry, "from_json")


def test_unverified_says_why():
    p = Provenance.unverified("hub does not sign yet")
    assert p.verified is False
    assert "UNVERIFIED" in p.label()
    assert "hub does not sign yet" in p.label()


def test_message_round_trip():
    original = Message(
        seq=7,
        kind="ask",
        sender="bench/firmware",
        recipient="compute/analysis",
        body="do the failures correlate with temperature?",
        correlation_id="q-1",
        artifacts=(Artifact("bench", "/srv/x.bin", sha256="abc", size_bytes=12),),
    )
    assert Message.from_json(original.to_json()) == original


def test_agent_round_trip():
    original = Agent(name="a/b", machine="m", cwd="/tmp", capabilities=("hil", "jtag"))
    assert Agent.from_json(original.to_json()) == original


def test_unknown_kind_is_rejected():
    with pytest.raises(WireError, match="unknown message kind"):
        Message.from_json({"kind": "shout", "sender": "a", "recipient": "b", "body": "x"})


def test_oversized_body_points_at_artifacts():
    payload = {"kind": "tell", "sender": "a", "recipient": "b", "body": "x" * (MAX_BODY_CHARS + 1)}
    with pytest.raises(WireError, match="artifact"):
        Message.from_json(payload)


def test_missing_field_names_itself():
    with pytest.raises(WireError, match="'sender'"):
        Message.from_json({"kind": "tell", "recipient": "b", "body": "x"})


def test_version_mismatch_is_refused():
    with pytest.raises(WireError, match="protocol"):
        check_version({"v": PROTOCOL_VERSION + 1})


def test_unversioned_payload_is_tolerated():
    check_version({})


def test_loads_rejects_non_objects():
    with pytest.raises(WireError, match="JSON object"):
        loads(b"[1, 2, 3]")


def test_dumps_loads_round_trip():
    assert loads(dumps({"v": PROTOCOL_VERSION, "a": 1}))["a"] == 1
