"""Signing your own sends, and the three answers `cairn sent` can now give.

The first cut of docs/design.md §12 item 9. What is asserted here is mostly
**what a signature does not prove**, because the failure this surface is built
against is cairn claiming more than it checked, and a round-trip test on its own
would pass just as happily over a scheme that covered nothing.

Two of these are absences that a well-meaning patch removes: that `seq` and
`created_at` are outside the signature, and that a missing signature and a
failing one do not share a verdict.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cairn import provenance, render, signing
from cairn.config import key_file
from cairn.wire import Artifact, Message, Provenance, SentEntry


def _draft(**kwargs) -> Message:
    fields = {
        "seq": 0,
        "kind": "tell",
        "sender": "bench/firmware",
        "recipient": "compute/traces",
        "body": "soak 441 failed 3 of 40",
        "created_at": "2026-08-04T09:00:00Z",
    }
    return Message(**{**fields, **kwargs})


def _signed(message: Message, cwd=None) -> Message:
    return replace(message, signature=signing.sign(message, cwd))


# -- the key -----------------------------------------------------------------


def test_a_key_is_created_on_first_use_and_then_reused(tmp_path):
    """Created rather than required: a key somebody has to set up is a key most machines lack.

    The reuse half is the one with teeth. A `key()` that minted a fresh secret
    per call would sign and verify perfectly inside one process and report every
    send from every earlier session as `MISMATCH` — the loudest verdict cairn has,
    on the most ordinary situation there is.
    """
    first = signing.key(tmp_path)

    assert len(first) == 32
    assert signing.key(tmp_path) == first, "a second call minted a new key and orphaned every earlier send"


def test_the_key_file_is_not_world_readable(tmp_path):
    signing.key(tmp_path)

    assert key_file(tmp_path).stat().st_mode & 0o077 == 0, "the only secret cairn writes was readable by others"


def test_two_directories_do_not_share_a_key(tmp_path):
    """Which is what makes a takeover from elsewhere fail to verify, and it should."""
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()

    assert signing.key(one) != signing.key(two)


# -- what the signature covers, and what it does not -------------------------


def test_a_signature_this_directory_made_verifies(tmp_path):
    assert signing.verify(_signed(_draft(), tmp_path), tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"body": "soak 441 passed 40 of 40"},
        {"recipient": "compute/other"},
        {"kind": "ask"},
        {"sender": "someone/else"},
        {"correlation_id": "q-forged"},
        {"artifacts": (Artifact(host="bench", path="/srv/added.bin"),)},
    ],
    ids=["body", "recipient", "kind", "sender", "correlation_id", "artifacts"],
)
def test_editing_a_covered_field_breaks_the_signature(tmp_path, change):
    """One case per covered field, because a signature over five of six is a signature over none.

    `signing.canonical` writes the list out by hand rather than deriving it from
    `Message`, so the list and this parametrization are the two halves of that
    decision. A field added to the schema and not to both is the failure.
    """
    signed = _signed(_draft(), tmp_path)

    assert not signing.verify(replace(signed, **change), tmp_path)


@pytest.mark.parametrize(
    "change",
    [{"seq": 4111}, {"created_at": "1999-01-01T00:00:00Z"}, {"retracted_at": "2026-08-04T10:00:00Z"}],
    ids=["seq", "created_at", "retracted_at"],
)
def test_the_fields_the_hub_owns_are_outside_the_signature(tmp_path, change):
    """Not a gap — a limit, and the one most likely to be quietly closed by "improving" canonical.

    The hub assigns `seq` and `created_at` (`store.append` calls `now()` itself)
    and sets `retracted_at` later, so a sender has nothing to sign them with.
    Pulling them in would make every signature fail the moment it came back from
    the hub, which is the bug this test exists to describe rather than to permit.

    The consequence is real and is why `assess_sent` says it out loud: a hub that
    re-dated or re-ordered your sends hands back rows that verify.
    """
    signed = _signed(_draft(), tmp_path)

    assert signing.verify(replace(signed, **change), tmp_path)


def test_a_signature_from_another_directory_does_not_verify_here(tmp_path):
    """The takeover case, which is the ordinary way a real reading becomes mixed."""
    predecessor, successor = tmp_path / "one", tmp_path / "two"
    predecessor.mkdir()
    successor.mkdir()

    theirs = _signed(_draft(), predecessor)

    assert not signing.verify(theirs, successor)


# -- the three verdicts ------------------------------------------------------


def test_no_signature_is_not_the_same_finding_as_a_bad_one(tmp_path, monkeypatch):
    """The distinction the third verdict exists for, and the one worth losing sleep over.

    An absent signature is evidence of nothing: that row predates the build, or
    crossed a hub too old to store one. A failing signature is evidence of
    something. Giving both the same word hands the loudest row on the page the
    vocabulary of the most ordinary one.
    """
    monkeypatch.chdir(tmp_path)
    absent = provenance.assess_sent(_draft())
    wrong = provenance.assess_sent(replace(_draft(), signature="00" * 32))

    assert absent.token() == "UNVERIFIED"
    assert wrong.token() == "MISMATCH"
    assert absent.detail != wrong.detail


def test_a_send_this_directory_made_reads_back_verified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    verdict = provenance.assess_sent(_signed(_draft()))

    assert verdict.verified
    assert verdict.token() == f"verified({signing.METHOD})"


def test_a_pass_still_says_what_it_did_not_check(tmp_path, monkeypatch):
    """A verdict with its qualification dropped is the overclaim this surface exists to avoid.

    Asserted against the **rendered page** rather than against `detail`, and the
    move is the point. This first pinned the sentence to one field, and when the
    acceptance run showed the footer was too far from the rows to stop an
    ordering claim, the sentence moved to the banner and this went red — a test
    reporting a relocation as a regression. What has to hold is that a reader
    sees the limit somewhere on the page they are reading, not that a particular
    string sits in a particular slot, so it now pins the guarantee where the
    reader meets it and any future move stays free.
    """
    monkeypatch.chdir(tmp_path)
    signed = _signed(_draft(seq=1))
    page = render.sent_text([SentEntry(message=signed, provenance=provenance.assess_sent(signed))], 1)

    assert "never the sequence or the time" in page, "the page printed a pass without its limit"
    assert page.count("never the sequence or the time") == 1, "said twice; tier 3 is once per reading"


def test_the_inbox_and_notes_verdicts_did_not_move(tmp_path, monkeypatch):
    """Only the local half landed: a peer's signature is not checkable and must not read as if it were."""
    monkeypatch.chdir(tmp_path)
    signed_by_someone = replace(_draft(), signature="00" * 32)

    assert provenance.assess(signed_by_someone).token() == "UNVERIFIED"
    assert "sender identity is asserted" in provenance.assess(signed_by_someone).detail


def test_a_peers_signature_is_named_as_uncheckable_rather_than_left_to_look_like_proof(tmp_path, monkeypatch):
    """The `verified_by` failure, re-enabled by this cut and closed in the same one.

    `provenance`'s module docstring is built on an agent refusing a
    `verified_by: "cairn-hub"` field because nothing verified it. Putting a real
    signature on the wire hands the inbox a cryptographic-looking field that
    nobody on this machine can check — the same shape, better dressed — so the
    reading has to say which it is.
    """
    monkeypatch.chdir(tmp_path)
    plain = provenance.assess(_draft())
    from_a_peer = provenance.assess(replace(_draft(), signature="00" * 32))

    assert "this machine does not have" in from_a_peer.detail
    assert from_a_peer.detail != plain.detail, "a signed peer row explained itself the same as an unsigned one"
    assert "signature" not in plain.detail, "the clause fired on a row that has none, which is how it becomes furniture"


# -- the reading -------------------------------------------------------------


def _page(*provenances: Provenance) -> str:
    entries = [
        SentEntry(message=_draft(seq=n + 1, body=f"update {n + 1}"), provenance=p) for n, p in enumerate(provenances)
    ]
    return render.sent_text(entries, len(entries))


_UNSIGNED = Provenance.unverified("no signature on this one — sent before this build")
_VERIFIED = Provenance(verified=True, method=signing.METHOD, detail="covers the words, not the sequence or the time")
_MISMATCH = Provenance.mismatch(signing.METHOD, "this directory's key does not reproduce it")


def test_a_page_that_is_all_unverified_says_nothing_new():
    """Silence on the case that has always been the case. A warning on every page is furniture again."""
    assert "⚠" not in _page(_UNSIGNED, _UNSIGNED)


def test_a_mixed_page_announces_itself_before_the_first_row():
    """The constraint docs/design.md §12 item 18 left on this cut.

    Above the rows, not in the footer: item 18 noted the count line is what
    `head` keeps and the footer is what it cuts, and `SKILL.md` telling readers
    not to pipe through `head` is evidence that they do.
    """
    lines = _page(_UNSIGNED, _VERIFIED, _MISMATCH).splitlines()
    warning = next(i for i, line in enumerate(lines) if "⚠" in line)
    first_row = next(i for i, line in enumerate(lines) if line.startswith("seq "))

    assert warning < first_row
    assert "1 UNVERIFIED" in lines[warning]
    assert f"1 verified({signing.METHOD})" in lines[warning]
    assert "1 MISMATCH" in lines[warning]


def test_an_all_verified_page_announces_itself_too():
    """Broader than item 18's literal wording, and deliberately.

    The risk it recorded is a reader missing the *change*: *"the people most
    fluent in this tool are the ones least likely to notice"*. An all-verified
    page is the largest change this surface has ever undergone and the one where
    every line looks reassuring.
    """
    assert "⚠" in _page(_VERIFIED, _VERIFIED)


def test_the_warning_names_which_rows_are_weaker_not_just_how_many():
    """A count tells a reader to go and scan. The acceptance run measured what that costs.

    The first blind reading quoted the warning back verbatim and still summarised
    six rows as one uniform block, because *"the warning tells you 3 and 3 but not
    which three — you have to scan rows to learn that the unsigned ones were the
    substance"*. They were: on that page the failure report, the question and the
    workaround were the unsigned three.
    """
    warning = next(line for line in _page(_UNSIGNED, _UNSIGNED, _VERIFIED).splitlines() if "⚠" in line)

    assert warning.endswith("seq 1, 2"), "the list is the rows a reader may claim least about, and only those"


def test_the_warning_enumerates_and_never_collapses_to_a_range():
    """`seq 1-3` is shorter and is the exact error this line exists to deny.

    The acceptance reader named it against itself: *"collapsing an enumeration
    into a range is an assertion of uniformity, and I did it over rows the output
    had told me four lines earlier were not uniform."* A line whose job is to
    refuse that assertion must not make it in its own words.
    """
    warning = next(line for line in _page(_UNSIGNED, _UNSIGNED, _UNSIGNED, _VERIFIED).splitlines() if "⚠" in line)

    assert "seq 1, 2, 3" in warning
    assert "1-3" not in warning
    assert "1\u20133" not in warning, "an en dash is still a range"


def test_a_long_page_caps_the_list_and_says_what_it_dropped():
    """Silent truncation, on the page about how much can be trusted, of all places."""
    warning = next(line for line in _page(*([_UNSIGNED] * 10), _VERIFIED).splitlines() if "⚠" in line)

    assert "seq 1, 2, 3, 4, 5, 6 and 4 more" in warning


def test_the_coverage_limit_is_beside_the_warning_and_said_once():
    """Fix 3: the footer was maximally far from the rows whose sequence was being quoted."""
    lines = _page(_UNSIGNED, _VERIFIED).splitlines()
    warning = next(i for i, line in enumerate(lines) if "⚠" in line)
    first_row = next(i for i, line in enumerate(lines) if line.startswith("seq "))
    coverage = next(i for i, line in enumerate(lines) if "never the sequence or the time" in line)

    assert warning < coverage < first_row, "the limit is not between the warning and the rows it qualifies"
    assert "\n".join(lines).count("never the sequence or the time") == 1


def test_the_summary_does_not_replace_the_per_row_verdict():
    """Moving the verdict into the footnote is what `test_render.py` calls "the cheap mistake".

    A summary at the top makes the per-row verdicts look redundant, which is the
    same mistake wearing a friendlier face. Tier 1 is unchanged: the count line
    counts the rows rather than standing in for them.
    """
    text = _page(_UNSIGNED, _VERIFIED)

    assert sum(line.startswith("seq ") for line in text.splitlines()) == 2
    assert text.count("UNVERIFIED") > 1, "the rows lost their own verdict once the page had a summary"


def test_a_fully_verified_page_stops_claiming_nothing_was_proven():
    """A footnote saying the hub's word is all you have, printed directly under the proof."""
    assert render.RECORD_CLAUSE not in _page(_VERIFIED, _VERIFIED)
    assert render.RECORD_CLAUSE in _page(_VERIFIED, _UNSIGNED)
