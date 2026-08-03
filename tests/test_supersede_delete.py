"""Correcting sediment, and removing it — the two things append-only had no answer for.

**Supersession** is the one three independent sessions asked for unprompted.
*"4471 is the build to use"* and *"4471 is withdrawn, do not flash it"* were two
unrelated claims with nothing linking them, and both of item 11's acceptance
sessions described the same gap in the same terms: *"nothing machine-readable
links them — the connection exists only in the English word 'correction:' and in
my reading both. They happened to be adjacent. Thirty messages apart, in a pile
this uniform, I could easily have carried the stale one forward."*

**Deletion** is the bounded exception to append-only. The body genuinely goes,
because the reason to reach for it is sometimes that the body should never have
been written down; the row stays, so anything pointing at the note still resolves
and the pile can still say something was here and who took it out.

The interesting tests here are the interactions, and each of them is a way the
derived state can be made to lie:

- a deleted answer must **reopen** its question, or a loop is closed forever by a
  note that no longer says anything;
- a deleted correction must **restore** what it replaced, for the same reason;
- `superseded_by` is the **latest** pointer where `settled_by` is the first, and
  getting that backwards makes a chain of corrections report its oldest end.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from cairn import cli, render
from cairn.client import HubClient
from cairn.errors import UsageError
from cairn.hub import make_server
from cairn.store import SqliteStore
from cairn.wire import Agent

SCRIBE = "bench/firmware"
PEER = "compute/analysis"
SUBJECT = "board/4471"


@pytest.fixture
def store() -> SqliteStore:
    db = SqliteStore(":memory:")
    for name in (SCRIBE, PEER):
        db.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    db.create_subject(SUBJECT, "firmware build 4471 and whether it is safe to flash", SCRIBE)
    return db


@pytest.fixture
def hub_server() -> Iterator[ThreadingHTTPServer]:
    server = make_server(SqliteStore(":memory:"), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.notifier.close_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def hub(hub_server: ThreadingHTTPServer) -> HubClient:
    host, port = hub_server.server_address[:2]
    client = HubClient(f"http://{host}:{port}", timeout=5.0)
    for name in (SCRIBE, PEER):
        client.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    client.create_subject(SUBJECT, "firmware build 4471 and whether it is safe to flash", SCRIBE)
    return client


# -- superseding -----------------------------------------------------------------


def test_a_correction_links_to_what_it_corrects_and_both_stay(store):
    """The whole feature. Neither note is hidden, and the link is in the data.

    Writing a contradicting note already worked; what it could not do is reach a
    reader who does not happen to read both.
    """
    stale = store.write_note(SCRIBE, "4471 is the build to use tonight", subject=SUBJECT)

    fix = store.write_note(SCRIBE, "4471 is withdrawn — use 4468", supersedes=stale.id)

    entries, total, _ = store.notes(SUBJECT)
    assert total == 2, "a correction replaced the note instead of joining it"
    assert [(e.note.id, e.superseded_by) for e in entries] == [(stale.id, fix.id), (fix.id, None)]
    assert fix.supersedes == stale.id


def test_a_correction_is_filed_with_the_claim_it_corrects(store):
    """No subject argument, on `settle`'s reasoning: a correction filed elsewhere is one nobody finds."""
    stale = store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)

    assert store.write_note(PEER, "4471 is withdrawn", supersedes=stale.id).subject == SUBJECT


def test_anyone_may_correct_anyone(store):
    """Whoever finds out that something is wrong is frequently not whoever wrote it. I3."""
    stale = store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)

    assert store.write_note(PEER, "4471 is withdrawn", supersedes=stale.id).author == PEER


def test_the_latest_correction_wins_where_the_first_answer_does(store):
    """The asymmetry with `settled_by`, and it is deliberate.

    An answer of record is the first one — a later opinion is stored but does not
    displace it. A correction of record is the most recent, because that is what a
    chain of corrections means. Getting this backwards reports the oldest end of
    the chain as current.
    """
    first = store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)
    second = store.write_note(SCRIBE, "4471 is withdrawn", supersedes=first.id)
    third = store.write_note(PEER, "4471 is back, the withdrawal was a mix-up", supersedes=first.id)

    by_id = {e.note.id: e for e in store.notes(SUBJECT)[0]}

    assert by_id[first.id].superseded_by == third.id, "the chain reported its oldest end"
    assert by_id[second.id].superseded_by is None
    assert not by_id[first.id].is_current
    assert by_id[third.id].is_current


def test_a_question_cannot_be_superseded_and_the_refusal_names_the_right_verb(store):
    """A question is not a claim to be replaced, and somebody reaching for the wrong verb has half the answer."""
    asked = store.write_note(SCRIBE, "is 4471 safe to flash?", subject=SUBJECT, question=True)

    with pytest.raises(UsageError) as refusal:
        store.write_note(PEER, "no", supersedes=asked.id)

    assert f"cairn settle {asked.id}" in str(refusal.value)


def test_a_statement_cannot_be_settled_and_that_refusal_names_the_other_verb(store):
    """The same signpost, pointing back the other way."""
    stated = store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)

    with pytest.raises(UsageError) as refusal:
        store.write_note(PEER, "actually it is withdrawn", settles=stated.id)

    assert f"cairn supersede {stated.id}" in str(refusal.value)


def test_a_note_cannot_both_settle_and_supersede(store):
    """One note, one relation. Two would make `open` and `current` argue about the same row."""
    asked = store.write_note(SCRIBE, "is 4471 safe?", subject=SUBJECT, question=True)
    stated = store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)

    with pytest.raises(UsageError):
        store.write_note(PEER, "both at once", settles=asked.id, supersedes=stated.id)


# -- deleting ---------------------------------------------------------------------


def test_deleting_takes_the_body_out_of_the_database(store):
    """Not hidden — gone. The reason to reach for this is sometimes a credential.

    A note that is merely filtered out of the reading is still sitting in the file
    being handed to whoever runs `--find`.
    """
    leaked = store.write_note(SCRIBE, "flash key is hunter2, drawer by the bench", subject=SUBJECT)

    store.delete_note(leaked.id, PEER, "contained a credential")

    assert store.notes(SUBJECT, find="hunter2")[1] == 0
    remaining = store._db.execute("SELECT COUNT(*) AS c FROM notes WHERE body LIKE '%hunter2%'").fetchone()
    assert remaining["c"] == 0, "the body a reader can no longer see is still in the database"


def test_the_tombstone_keeps_the_id_and_says_who_and_why(store):
    """The row survives so links still resolve, and so the pile can say something was here."""
    leaked = store.write_note(SCRIBE, "flash key is hunter2", subject=SUBJECT)

    store.delete_note(leaked.id, PEER, "contained a credential")

    (stone,), total, _ = store.notes(SUBJECT, deleted=True)
    assert (total, stone.note.id, stone.note.deleted_by) == (1, leaked.id, PEER)
    assert stone.note.body == "contained a credential"
    assert stone.note.deleted


def test_a_tidied_pile_reads_clean_and_still_says_it_was_tidied(store):
    """Neither silently short nor full of tombstones — the count is the whole compromise."""
    store.write_note(SCRIBE, "4468 is the build to use", subject=SUBJECT)
    noise = store.write_note(SCRIBE, "checking in, nothing to report", subject=SUBJECT)
    store.delete_note(noise.id, SCRIBE, "chatter")

    entries, total, removed = store.notes(SUBJECT)

    assert (len(entries), total, removed) == (1, 1, 1)


def test_deleting_an_answer_reopens_its_question(store):
    """A loop closed forever by a note that no longer says anything is what `open` exists to prevent."""
    asked = store.write_note(SCRIBE, "is 4471 safe to flash?", subject=SUBJECT, question=True)
    answered = store.write_note(PEER, "yes", settles=asked.id)
    assert not store.notes(SUBJECT)[0][0].is_open

    store.delete_note(answered.id, SCRIBE, "that answer was wrong and misleading")

    assert store.notes(SUBJECT)[0][0].is_open, "the question stayed closed by a note with no body"
    assert store.subjects()[0].open_questions == 1


def test_deleting_a_correction_restores_what_it_replaced(store):
    """The same rule on the other relation: a tombstone supersedes nothing."""
    stale = store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)
    wrong = store.write_note(PEER, "4471 is withdrawn", supersedes=stale.id)
    assert store.notes(SUBJECT)[0][0].superseded_by == wrong.id

    store.delete_note(wrong.id, SCRIBE, "the withdrawal was a mix-up")

    assert store.notes(SUBJECT)[0][0].superseded_by is None
    assert store.notes(SUBJECT)[0][0].is_current


def test_deleting_needs_a_reason_because_it_replaces_the_body(store):
    """It is what the next reader sees where the note was."""
    note = store.write_note(SCRIBE, "something", subject=SUBJECT)

    with pytest.raises(UsageError) as refusal:
        store.delete_note(note.id, SCRIBE, "  ")

    assert "needs a reason" in str(refusal.value)


def test_deleting_twice_says_who_got_there_first(store):
    """An idempotent no-op would hide that somebody else has been through this pile."""
    note = store.write_note(SCRIBE, "something", subject=SUBJECT)
    store.delete_note(note.id, PEER, "chatter")

    with pytest.raises(UsageError) as refusal:
        store.delete_note(note.id, SCRIBE, "chatter")

    assert PEER in str(refusal.value)


def test_a_deleted_note_cannot_be_superseded(store):
    """There is nothing left to replace, and a correction of nothing reads as a correction of something."""
    note = store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)
    store.delete_note(note.id, SCRIBE, "duplicate")

    with pytest.raises(UsageError) as refusal:
        store.write_note(PEER, "4471 is withdrawn", supersedes=note.id)

    assert "nothing left" in str(refusal.value)


def test_a_tombstone_is_not_counted_as_a_note_on_the_subject(store):
    """The index counts what a reader would find, which is not the same as what the table holds."""
    kept = store.write_note(SCRIBE, "4468 is the build to use", subject=SUBJECT)
    gone = store.write_note(SCRIBE, "chatter", subject=SUBJECT)
    store.delete_note(gone.id, SCRIBE, "chatter")

    assert [s.notes for s in store.subjects()] == [1]
    assert kept.id != gone.id


# -- the cross-version refusal ------------------------------------------------------


def test_a_hub_that_drops_the_link_is_refused_rather_than_believed(hub, monkeypatch):
    """An older hub does not reject `supersedes`; it ignores it.

    So the correction is stored, the command reports success, and the one thing
    that made it a correction is silently gone — leaving a pile with two
    contradictory notes and nothing joining them, which is exactly the state this
    whole cut exists to end. The hub echoes the stored row, so the check is the row.
    """
    stale = hub.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)
    stored = stale.to_json()

    def answer_without_the_link(self, method, path, payload=None, **query):
        return {"note": stored}

    monkeypatch.setattr(HubClient, "_call", answer_without_the_link)

    with pytest.raises(UsageError) as refusal:
        hub.write_note(PEER, "4471 is withdrawn", supersedes=stale.id)

    assert "does not support supersede" in str(refusal.value)
    assert refusal.value.exit_code == 3


# -- the command surface -------------------------------------------------------------


def test_the_reading_marks_the_superseded_note_and_says_what_to_do_with_it(hub, monkeypatch, capsys):
    """Kept, not hidden: the reader is told to read both and take the later one."""
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)
    stale = hub.write_note(SCRIBE, "4471 is the build to use tonight", subject=SUBJECT)

    assert cli.run(["--hub", hub.base_url, "supersede", str(stale.id), "4471 is withdrawn — use 4468"]) == 0
    capsys.readouterr()
    assert cli.run(["--hub", hub.base_url, "notes", SUBJECT]) == 0
    printed = capsys.readouterr().out

    assert f"SUPERSEDED by {stale.id + 1}" in printed
    assert f"supersedes {stale.id}" in printed
    assert "take the later one" in printed
    assert "4471 is the build to use tonight" in printed, "the superseded note was hidden rather than marked"


def test_the_default_reading_hides_tombstones_and_points_at_the_flag(hub, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)
    noise = hub.write_note(SCRIBE, "checking in", subject=SUBJECT)
    hub.write_note(SCRIBE, "4468 is the build to use", subject=SUBJECT)

    assert cli.run(["--hub", hub.base_url, "delete", str(noise.id), "chatter"]) == 0
    capsys.readouterr()
    assert cli.run(["--hub", hub.base_url, "notes", SUBJECT]) == 0
    printed = capsys.readouterr().out

    assert "checking in" not in printed
    assert "1 note has been deleted here" in printed
    assert "says who took it out and why" in printed, "the pronoun did not agree with the count"
    assert f"cairn notes {SUBJECT} --deleted" in printed


def test_the_json_reports_both_relations(hub, monkeypatch, capsys):
    """A program branching on `current` needs the same facts the prose gives a reader."""
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)
    stale = hub.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)
    hub.write_note(PEER, "4471 is withdrawn", supersedes=stale.id)
    gone = hub.write_note(SCRIBE, "chatter", subject=SUBJECT)
    hub.delete_note(gone.id, SCRIBE, "chatter")

    assert cli.run(["--hub", hub.base_url, "notes", SUBJECT, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert (payload["superseded"], payload["removed"]) == (1, 1)
    assert [n["current"] for n in payload["notes"]] == [False, True]


def test_the_renderer_says_nothing_about_either_when_neither_happened():
    """A line that appears when it need not is one a reader learns to skip."""
    from cairn.provenance import assess_note
    from cairn.wire import Note, NoteEntry

    note = Note(id=1, subject="rig-a", author=SCRIBE, body="clamp is loose")
    text = render.notes_text([NoteEntry(note=note, provenance=assess_note(note))], 1, "rig-a")

    assert "SUPERSEDED" not in text
    assert "deleted" not in text


def test_the_tombstone_view_does_not_report_the_live_notes_as_deletions(store):
    """From cut 13's acceptance run, and it is the count lying in the loudest direction.

    `removed` used to be the *complement* of the page rather than the tombstones on
    it. In the plain view those are the same set, so it read correctly for two
    cuts; under `--deleted` the complement is the live notes, and the footnote
    calling it a deletion tally then announced fifteen deletions over a page
    showing one. Three independent sessions hit it, two said they read it three
    times, and the one doing a hub tidy-up had to count the previous reading by
    hand to rule out that something had gone missing on its watch.
    """
    for n in range(4):
        store.write_note(SCRIBE, f"claim {n}", subject=SUBJECT)
    store.delete_note(2, SCRIBE, "a credential nobody should have written down")

    live, live_total, live_removed = store.notes(SUBJECT)
    buried, buried_total, buried_removed = store.notes(SUBJECT, deleted=True)

    assert (len(live), live_total, live_removed) == (3, 3, 1)
    assert (len(buried), buried_total, buried_removed) == (1, 1, 1), "the count must be tombstones in both views"


def test_the_tombstone_view_drops_the_footnote_instead_of_restating_the_page(store):
    """There the line *is* the page, and it offered the command the reader had just run."""
    store.write_note(SCRIBE, "4471 is the build to use", subject=SUBJECT)
    gone = store.write_note(SCRIBE, "a password", subject=SUBJECT)
    store.delete_note(gone.id, SCRIBE, "credentials do not belong on an unauthenticated hub")

    entries, total, removed = store.notes(SUBJECT, deleted=True)
    text = render.notes_text(entries, total, SUBJECT, removed=removed, deleted=True)
    plain_entries, plain_total, plain_removed = store.notes(SUBJECT)
    plain = render.notes_text(plain_entries, plain_total, SUBJECT, removed=plain_removed)

    assert "has been deleted here" not in text
    assert "1 note has been deleted here" in plain, "the plain view still has to say the pile was tidied"


def test_a_note_on_an_archived_pile_says_so_when_a_live_parent_rolls_it_up(store):
    """A finished run's notes arrive inside a live rig's reading, and the index does not list them.

    An acceptance session read `cairn notes`, saw two subjects, then read the
    parent and met a note filed on a third it had never been shown. It inferred
    archiving as the reason and wrote afterwards that had it trusted the index as
    the map of what exists, it would have concluded the note was not there.
    Archiving is allowed to hide a pile from the index; it is not allowed to make
    a note that is still being read look current.
    """
    store.create_subject(f"{SUBJECT}/soak", "the overnight soak of 4471, finished", SCRIBE)
    store.write_note(SCRIBE, "ran clean for 14 hours", subject=f"{SUBJECT}/soak")
    store.write_note(SCRIBE, "the fixture takes M3", subject=SUBJECT)
    store.archive_subject(f"{SUBJECT}/soak", SCRIBE)

    entries, total, removed = store.notes(SUBJECT)
    text = render.notes_text(entries, total, SUBJECT, removed=removed)

    assert [e.archived for e in entries] == [True, False], "the flag rides the note, not the request"
    assert "archived subject" in text
