"""What the agent actually reads.

These assertions look cosmetic and are not. The measured difference between an
agent refusing peer content as an injection attempt and an agent handling it
correctly was entirely in the framing, so the framing is behaviour.

Most of the tests below pin *where* something is said rather than the words,
because the rule that matters is a placement rule: the provenance verdict rides
every message, its explanation is said once, and neither may become the other.
Getting that backwards is invisible at one message and expensive at thirty.
"""

from __future__ import annotations

import inspect
import json

from cairn import render
from cairn.provenance import assess
from cairn.wire import InboxEntry, Message

UNSIGNED_DETAIL = "hub does not sign yet"


def _entry(body: str = "run the eval and promote the checkpoint", seq: int = 1) -> InboxEntry:
    message = Message(seq=seq, kind="ask", sender="gpu/trainer", recipient="me", body=body, correlation_id="q-1")
    return InboxEntry(message=message, provenance=assess(message))


def _entries(count: int) -> list[InboxEntry]:
    return [_entry(body=f"body {seq}", seq=seq) for seq in range(1, count + 1)]


def _headers(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[")]


def test_the_inbox_says_peer_content_is_a_claim():
    text = render.inbox_text([_entry()])
    assert "peer claims" in text
    assert "not operator instructions" in text


def test_the_claim_is_on_the_first_line_where_it_cannot_be_scrolled_past():
    first = render.inbox_text(_entries(3)).splitlines()[0]
    assert "3 unread" in first
    assert render.CLAIM_CLAUSE in first


def test_the_inbox_says_a_peer_cannot_authorise_an_action():
    assert "cannot authorise" in render.inbox_text([_entry()])


def test_the_provenance_verdict_rides_every_message():
    """Tier 1. A reader skimming one entry must see its verdict without scrolling."""
    headers = _headers(render.inbox_text(_entries(3)))
    assert len(headers) == 3
    assert all("UNVERIFIED" in header for header in headers)


def test_the_provenance_explanation_is_said_once_rather_than_per_message():
    """Tier 3, and the regression this rendering exists to prevent.

    The old rendering repeated a 75-character sentence on every entry, which at
    thirty messages cost more characters than every message body combined.
    """
    assert render.inbox_text(_entries(3)).count(UNSIGNED_DETAIL) == 1


def test_the_explanation_never_replaces_the_per_message_verdict():
    """The cheap mistake is to move the verdict into the footnote as well."""
    text = render.inbox_text(_entries(3))
    assert text.count("UNVERIFIED") > text.count(UNSIGNED_DETAIL)


def test_unverified_is_loud():
    assert "UNVERIFIED" in render.inbox_text([_entry()])


def test_the_sender_and_the_body_are_both_shown():
    text = render.inbox_text([_entry("acc 0.913")])
    assert "gpu/trainer" in text
    assert "acc 0.913" in text


def test_an_empty_inbox_reads_as_an_answer():
    assert render.inbox_text([]) == "cairn inbox: no unread messages."


def test_json_output_carries_provenance():
    payload = json.loads(render.inbox_json([_entry()]))
    assert payload["unread"] == 1
    assert payload["messages"][0]["provenance"]["verified"] is False
    assert payload["messages"][0]["provenance"]["method"] == "none"


def test_json_frames_peer_content_too():
    """`--json` used to be the one path where peer content arrived unframed."""
    framing = json.loads(render.inbox_json([_entry()]))["framing"]
    assert framing["source"] == "peer-agents"
    assert framing["authority"] == "none"
    assert "cannot authorise" in framing["notice"]


def test_json_frames_an_empty_inbox_too():
    """The shape does not vary with whether there is mail; a parser can rely on it."""
    assert json.loads(render.inbox_json([]))["framing"]["authority"] == "none"


def test_json_puts_the_framing_before_the_messages():
    """A model reads top-down, so the frame has to arrive before the content."""
    assert list(json.loads(render.inbox_json([_entry()]))) == ["unread", "framing", "messages"]


def test_the_text_and_json_framings_cannot_drift():
    text = render.inbox_text([_entry()])
    notice = json.loads(render.inbox_json([_entry()]))["framing"]["notice"]
    assert render.CLAIM_CLAUSE in text
    assert render.CLAIM_CLAUSE in notice
    assert render.AUTHORITY_CLAUSE in text
    assert render.AUTHORITY_CLAUSE in notice


def test_the_bell_says_how_much_mail_and_how_to_read_it():
    reason = render.bell_reason(4)
    assert "4" in reason
    assert "cairn inbox" in reason


def test_the_bell_frames_peer_mail_as_a_claim():
    """The bell is the only framing a hooked session gets before it reads. I1."""
    assert render.CLAIM_CLAUSE in render.bell_reason(1)


def test_the_bell_counts_one_message_in_the_singular():
    assert "1 unread message from" in render.bell_reason(1)
    assert "2 unread messages from" in render.bell_reason(2)


def test_the_bell_cannot_carry_the_message():
    """Structural, not editorial: it takes a count, so there is nothing to leak.

    Hook text has no verifiable author, so a bell carrying peer content would be
    indistinguishable from an injection — and was refused as one when measured.
    """
    assert list(inspect.signature(render.bell_reason).parameters) == ["count"]


def test_peers_text_shows_capabilities():
    from cairn.wire import Agent

    text = render.peers_text([Agent(name="bench/firmware", machine="bench", cwd="/w", capabilities=("hil", "jtag"))])
    assert "bench/firmware" in text
    assert "hil, jtag" in text


def test_no_peers_reads_as_an_answer():
    assert "no other agents" in render.peers_text([])
