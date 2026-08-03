"""A subject is a thing somebody opened, not a string somebody typed.

It used to be the second. `cairn note soak-441 "…"` created `soak-441` as a side
effect, so `soak-441`, `eval-441`, `run-441` and `441` were four piles the hub
would open without comment, and opening one looked exactly like adding to one.
Measured rather than feared: an acceptance session invented `run-442` beside
existing notes about run 441 and said so itself — *"someone searching run-441
won't roll up into it."*

What this file pins is mostly **refusals**, because that is where the change
lives. A subject you have to open is only better than one you do not if the
refusal tells you what already exists; otherwise it is a speed bump that ends in
the same new pile one command later. So the refusal guesses, and when it cannot
guess it lists, and it always prints the command with the name already in it.

Two absences matter as much as the refusals. A pile with **no notes yet** has to
appear in the index — it is the one moment the index can stop a fifth spelling,
and grouping over notes could not represent it. And archiving has to **hide
without deleting**: the notes stay, the read still works, and `--archived` still
lists it.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from cairn import cli
from cairn.client import HubClient
from cairn.errors import UsageError
from cairn.hub import make_server
from cairn.store import BACKFILLED_AUTHOR, SqliteStore
from cairn.wire import Agent

SCRIBE = "bench/firmware"


@pytest.fixture
def hub_server() -> Iterator[ThreadingHTTPServer]:
    """Serve a hub on an ephemeral port, backed by an in-memory database."""
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
    """Return a client pointed at the running hub."""
    host, port = hub_server.server_address[:2]
    return HubClient(f"http://{host}:{port}", timeout=5.0)


@pytest.fixture
def store() -> SqliteStore:
    db = SqliteStore(":memory:")
    db.register(Agent(name=SCRIBE, machine="bench", cwd="/w/fw"))
    return db


# -- the refusal, which is the feature -----------------------------------------


def test_a_note_to_a_pile_nobody_opened_is_refused(store):
    """The whole change in one assertion."""
    with pytest.raises(UsageError) as refusal:
        store.write_note(SCRIBE, "3 of 40 iterations failed", subject="soak-441")

    assert "no subject 'soak-441'" in str(refusal.value)


def test_the_refusal_guesses_the_pile_the_writer_meant(store):
    """A bare run number is what somebody types when the pile is filed under a longer name.

    `difflib` scores `441` against `soak-441` at 0.55 — under any cutoff loose
    enough to be useful — because most of the candidate is the part the writer
    left out. Substring matching goes first for exactly this case; see
    `store._nearest`.
    """
    store.create_subject("soak-441", "overnight soak of build 441", SCRIBE)

    with pytest.raises(UsageError) as refusal:
        store.write_note(SCRIBE, "another result", subject="441")

    assert "did you mean: soak-441" in str(refusal.value)


def test_the_refusal_guesses_upwards_too(store):
    """Typing a child when only the parent exists is the same shape, reversed."""
    store.create_subject("rig-a", "thermal chamber A", SCRIBE)

    with pytest.raises(UsageError) as refusal:
        store.write_note(SCRIBE, "the door seal is perished", subject="rig-a/chamber")

    assert "did you mean: rig-a" in str(refusal.value)


def test_with_nothing_to_guess_the_refusal_lists_what_exists(store):
    """A guess it cannot make must not become silence: the list is the fallback."""
    store.create_subject("rig-a", "thermal chamber A", SCRIBE)
    store.create_subject("compute-b", "the analysis box", SCRIBE)

    with pytest.raises(UsageError) as refusal:
        store.write_note(SCRIBE, "unrelated", subject="zzz")

    assert "subjects that exist: compute-b, rig-a" in str(refusal.value)


def test_the_refusal_always_prints_the_command_that_fixes_it(store):
    """With the name already in it, because retyping is where a fifth spelling comes from."""
    with pytest.raises(UsageError) as refusal:
        store.write_note(SCRIBE, "3 of 40 failed", subject="soak-441")

    assert 'cairn subject soak-441 "<one line saying what it is>"' in str(refusal.value)


def test_a_child_is_its_own_pile_and_needs_its_own_opening(store):
    """The maintainer's call, and the narrower of the two readings.

    The measured sprawl was all *roots* — `soak-441`, `eval-441`, `run-441`,
    `441` — so letting a child through on its parent's authority would have been
    defensible. One rule is easier to state and to trust, and relaxing a refusal
    later is backwards-compatible where tightening one is not.
    """
    store.create_subject("rig-a", "thermal chamber A", SCRIBE)

    with pytest.raises(UsageError):
        store.write_note(SCRIBE, "the door seal is perished", subject="rig-a/chamber")

    store.create_subject("rig-a/chamber", "the chamber itself", SCRIBE)
    assert store.write_note(SCRIBE, "the door seal is perished", subject="rig-a/chamber").subject == "rig-a/chamber"


# -- opening one ----------------------------------------------------------------


def test_a_subject_without_a_description_is_refused(store):
    """The description is the command's reason for existing, not its garnish.

    Counts tell a reader how much is on a pile. Only this tells them whether it is
    the pile they meant, which is the question being asked at the moment somebody
    is about to open a fifth one.
    """
    with pytest.raises(UsageError) as refusal:
        store.create_subject("rig-a", "   ", SCRIBE)

    assert "needs a description" in str(refusal.value)


def test_opening_the_same_pile_twice_is_refused_rather_than_treated_as_an_update(store):
    """Two writers describing one pile differently is the same divergence, smaller.

    And the second writer is usually somebody who did not know the first existed —
    precisely the reader this refusal is trying to catch.
    """
    store.create_subject("rig-a", "thermal chamber A", SCRIBE)

    with pytest.raises(UsageError) as refusal:
        store.create_subject("Rig-A", "the other thermal chamber", SCRIBE)

    assert "already exists" in str(refusal.value)


def test_a_pile_with_no_notes_on_it_is_still_in_the_index(store):
    """The one moment the index can prevent a duplicate, and the old shape could not show it.

    `GROUP BY` over notes can only report piles that already have sediment. A
    subject that somebody opened and described, and that nobody has written to
    yet, is exactly what the next writer needs to see.
    """
    store.create_subject("soak-442", "tonight's soak, not started yet", SCRIBE)

    index = store.subjects()

    assert [(s.subject, s.notes, s.description) for s in index] == [("soak-442", 0, "tonight's soak, not started yet")]


def test_a_subject_is_folded_on_opening_like_everywhere_else(store):
    """`Rig-A` and `rig-a` as two piles is the silent failure the fold exists to stop."""
    assert store.create_subject("  Rig-A  ", "thermal chamber A", SCRIBE).subject == "rig-a"


def test_an_unregistered_author_cannot_open_a_pile(store):
    """Same door as every other write: a name nobody can look up is not attribution."""
    with pytest.raises(UsageError):
        store.create_subject("rig-a", "thermal chamber A", "nobody/here")


# -- the upgrade ----------------------------------------------------------------


def test_an_existing_database_gets_its_subjects_backfilled(tmp_path):
    """There is a hub running in a container with real sediment on it.

    Nothing could have created these rows and nobody knows they are needed, so the
    schema repairs itself at open. Dated to the first note rather than to the
    upgrade, because the pile genuinely started then and dating it to now would
    make every existing subject look new on the one reading where age is the point.
    """
    path = tmp_path / "old.db"
    store = SqliteStore(path)
    store.register(Agent(name=SCRIBE, machine="bench", cwd="/w/fw"))
    store.create_subject("rig-a", "thermal chamber A", SCRIBE)
    first = store.write_note(SCRIBE, "clamp is loose", subject="rig-a")
    store._db.execute("DROP TABLE subjects")
    store.close()

    reopened = SqliteStore(path)
    pile = reopened.get_subject("rig-a")

    assert pile is not None, "a database with notes came back with no subject to write to"
    assert pile.notes == 1
    assert pile.created_by == BACKFILLED_AUTHOR
    opened = reopened._db.execute("SELECT created_at FROM subjects WHERE name = 'rig-a'").fetchone()
    assert opened["created_at"] == first.created_at, "the pile was dated to the upgrade rather than to its first note"
    assert "predates" in pile.description, "an invented description would be worse than an admitted gap"
    reopened.close()


# -- archiving ------------------------------------------------------------------


def test_archiving_hides_the_pile_and_keeps_every_note(store):
    """Hide and refuse; never delete, never conceal."""
    store.create_subject("soak-441", "overnight soak of build 441", SCRIBE)
    store.write_note(SCRIBE, "3 of 40 iterations failed", subject="soak-441")

    store.archive_subject("soak-441", SCRIBE)

    assert store.subjects() == [], "an archived pile is still in the default index"
    assert [s.subject for s in store.subjects(archived=True)] == ["soak-441"]
    assert [e.note.body for e in store.notes("soak-441")[0]] == ["3 of 40 iterations failed"]


def test_an_archived_pile_takes_no_new_notes_and_says_how_to_reopen(store):
    """Refusing rather than merely hiding is what makes somebody notice they are reopening finished work."""
    store.create_subject("soak-441", "overnight soak of build 441", SCRIBE)
    store.archive_subject("soak-441", SCRIBE)

    with pytest.raises(UsageError) as refusal:
        store.write_note(SCRIBE, "one more thing", subject="soak-441")

    assert "--reopen" in str(refusal.value)

    store.archive_subject("soak-441", SCRIBE, reopen=True)
    assert store.write_note(SCRIBE, "one more thing", subject="soak-441").subject == "soak-441"


def test_archiving_a_pile_with_an_open_question_is_refused(store):
    """The index is ordered by open questions; archiving would take them out of it.

    Closing finished work should mean looking at what is still open on it. The
    escape is one command — an answer of "no longer relevant" settles a question
    perfectly well.
    """
    store.create_subject("rig-a", "thermal chamber A", SCRIBE)
    asked = store.write_note(SCRIBE, "is the spare 2C high too?", subject="rig-a", question=True)

    with pytest.raises(UsageError) as refusal:
        store.archive_subject("rig-a", SCRIBE)

    assert "1 open question" in str(refusal.value)

    store.write_note(SCRIBE, "no longer relevant, run closed", settles=asked.id)
    assert store.archive_subject("rig-a", SCRIBE).archived


# -- the command surface ---------------------------------------------------------


def test_a_note_to_an_unopened_pile_exits_three_from_the_command_line(hub, monkeypatch, capsys):
    """Asserted through `cli.run`, because the exit code is the interface.

    3 is "cannot be carried out as asked". 1 would say "asked, nothing to report"
    and 2 would say the hub is down — on a hub that just answered.
    """
    hub.register(Agent(name=SCRIBE, machine="bench", cwd="/w/fw"))
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)

    assert cli.run(["--hub", hub.base_url, "note", "soak-441", "3 of 40 failed"]) == 3
    assert "no subject 'soak-441'" in capsys.readouterr().err

    assert cli.run(["--hub", hub.base_url, "subject", "soak-441", "overnight soak of build 441"]) == 0
    assert cli.run(["--hub", hub.base_url, "note", "soak-441", "3 of 40 failed"]) == 0


def test_the_index_says_what_each_pile_is_for(hub, monkeypatch, capsys):
    """The line a writer reads before deciding whether their pile already exists."""
    hub.register(Agent(name=SCRIBE, machine="bench", cwd="/w/fw"))
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)
    assert cli.run(["--hub", hub.base_url, "subject", "soak-441", "overnight soak of build 441 on rig A"]) == 0
    capsys.readouterr()

    assert cli.run(["--hub", hub.base_url, "notes"]) == 0

    assert "overnight soak of build 441 on rig A" in capsys.readouterr().out


def test_an_older_hub_says_so_rather_than_reading_as_an_outage(monkeypatch):
    """`client._call` maps a 404 to `Unreachable`, which is right for a garnish and wrong here.

    `_open_questions` and `_pile` swallow that and carry on, because they are
    decoration on a command that already worked. Here the route *is* the command,
    and "cannot reach hub" on a hub that just answered `cairn notes` sends the
    reader to check the network — the one place the fault is not.
    """
    from cairn.errors import Unreachable

    def missing_route(self, method, path, payload=None, **query):
        msg = "hub returned 404: no route /v1/subjects"
        raise Unreachable(msg)

    monkeypatch.setattr("cairn.client.HubClient._call", missing_route)
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)

    with contextlib.suppress(SystemExit):
        assert cli.run(["--hub", "http://127.0.0.1:1", "subject", "rig-a", "thermal chamber A"]) == 3


def test_the_index_says_how_many_archived_piles_it_is_not_showing(hub, monkeypatch, capsys):
    """From cut 13's acceptance run: hidden is fine, concealed is not.

    A session read `cairn notes`, was shown two subjects, then read a parent and
    met a note filed on a third it had never been offered. It worked out that the
    pile must be archived and wrote afterwards that had it trusted the index as
    the map of what exists — which is what the index looks like, and what this
    file calls the thing to run on arrival — it would have concluded the note was
    not there. Same rule as the tombstone count in §12 item 14: leave it out of
    the page, say that you did.
    """
    hub.register(Agent(name=SCRIBE, machine="bench", cwd="/w/fw"))
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)
    hub.create_subject("rig-a", "the thermal chamber rig", SCRIBE)
    hub.create_subject("rig-a/soak-441", "last quarter's soak, finished", SCRIBE)
    hub.write_note(SCRIBE, "ran clean for 14 hours", subject="rig-a/soak-441")
    hub.archive_subject("rig-a/soak-441", SCRIBE)
    capsys.readouterr()

    assert cli.run(["--hub", hub.base_url, "notes"]) == 0
    printed = capsys.readouterr().out

    assert "rig-a/soak-441" not in printed, "archiving still takes a finished pile out of the index"
    assert "1 archived subject not shown" in printed
    assert "--archived" in printed


def test_the_index_says_it_even_when_everything_left_is_archived(hub, monkeypatch, capsys):
    """The empty answer is the one most likely to be read as "there is nothing here"."""
    hub.register(Agent(name=SCRIBE, machine="bench", cwd="/w/fw"))
    monkeypatch.setenv("CAIRN_AGENT", SCRIBE)
    hub.create_subject("rig-a/soak-441", "last quarter's soak, finished", SCRIBE)
    hub.archive_subject("rig-a/soak-441", SCRIBE)
    capsys.readouterr()

    assert cli.run(["--hub", hub.base_url, "notes"]) == 1, "nothing to read is exit 1, as it always was"

    assert "1 archived subject" in capsys.readouterr().out
