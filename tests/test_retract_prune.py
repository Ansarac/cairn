"""Taking mail back out of the pipe, and clearing the pipe out.

Two commands with one thing in common: both remove, and both have to prove they
removed only what nobody was owed.

**`retract` works while the message is still in the mechanism and refuses when it
is not.** That refusal is the design rather than a limitation being apologised
for — once a cursor is past a message the words are in somebody's context, and a
command that reported success anyway would leave a sender believing something
untrue at exactly the moment that matters. What it says instead is *who* read it.

**`prune` deletes and must never take undelivered mail.** "The peer was switched
off for a week and got its backlog anyway" is the premise of the product, so the
one thing a cleanup cannot be allowed to do is break it.

The subtle tests are neither of the happy paths:

- a retracted message has to leave `unread`, `head` **and** `matching`, or the
  turn-boundary bell rings for mail that can never render and latches on a seq
  nothing will ever deliver — §12 item 6's deafness, rebuilt from the other side;
- a broadcast retraction is **partial**, so "it worked" and "it failed" are both
  wrong answers;
- pruning must count what it left behind, because a cleanup that quietly did less
  than asked is the silent shape this project keeps refusing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from cairn import cli, render
from cairn.client import HubClient
from cairn.errors import UsageError
from cairn.hub import make_server
from cairn.provenance import assess_sent
from cairn.store import SqliteStore
from cairn.wire import Agent, SentEntry

SENDER = "bench/firmware"
READER = "compute/analysis"
THIRD = "ops/dispatch"
LONG_AGO = "2026-01-01T00:00:00Z"


@pytest.fixture
def store() -> SqliteStore:
    db = SqliteStore(":memory:")
    for name in (SENDER, READER, THIRD):
        db.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
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
    for name in (SENDER, READER, THIRD):
        client.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    return client


def _age(store: SqliteStore) -> None:
    """Backdate every message, so a prune has something old to find."""
    store._db.execute("UPDATE messages SET created_at = ?", (LONG_AGO,))


# -- retracting ------------------------------------------------------------------


def test_a_message_nobody_has_read_stops_being_delivered(store):
    """The happy path, and the only one where cairn can actually take something back."""
    sent = store.append("tell", SENDER, READER, "flash board 4471 tonight")

    withdrawal = store.retract(sent.seq, SENDER)

    assert (withdrawal.withheld, withdrawal.read_by) == (1, ())
    assert store.unread(READER).messages == ()


def test_a_retracted_message_leaves_every_count_and_not_just_the_page(store):
    """The bell reads `unread` and latches on `head`, and neither is the page.

    A withdrawn message that still counted would ring for mail that can never
    render, then pin the latch to a seq nothing will ever deliver — which is
    exactly the deafness docs/design.md §12 item 6 removed, arriving from the
    other end. The clause lives in the shared predicate for that reason.
    """
    store.append("tell", SENDER, READER, "still live")
    pulled = store.append("tell", SENDER, READER, "withdrawn")

    store.retract(pulled.seq, SENDER)
    page = store.unread(READER)

    assert (page.unread, page.head, page.matching) == (1, 1, 1)
    assert [m.body for m in page.messages] == ["still live"]


def test_a_message_that_has_been_read_cannot_be_taken_back_and_says_who_read_it(store):
    """The refusal is the useful half: it turns "I cannot fix this" into "I know who to talk to"."""
    sent = store.append("tell", SENDER, READER, "flash board 4471 tonight")
    store.ack(READER, sent.seq)

    with pytest.raises(UsageError) as refusal:
        store.retract(sent.seq, SENDER)

    assert "too late" in str(refusal.value)
    assert READER in str(refusal.value)


def test_a_broadcast_retraction_is_partial_and_reports_both_halves(store):
    """One row, many mailboxes, one cursor each. "It worked" and "it failed" are both wrong."""
    sent = store.append("tell", SENDER, "*", "all hands")
    store.ack(READER, sent.seq)

    withdrawal = store.retract(sent.seq, SENDER)

    assert withdrawal.withheld == 1, "the mailbox that had not read it was not spared"
    assert withdrawal.read_by == (READER,)
    assert store.unread(THIRD).messages == ()


def test_only_the_sender_may_take_its_own_words_back(store):
    """The one place cairn checks ownership, and it is not the same question as `settle`'s.

    Settling and superseding *add* — anybody may, because whoever knows the answer
    is frequently not whoever asked. Retracting *removes*, and removing somebody
    else's message from a mailbox they were addressed in is not a correction, it is
    an interception.
    """
    sent = store.append("tell", SENDER, READER, "flash board 4471")

    with pytest.raises(UsageError) as refusal:
        store.retract(sent.seq, THIRD)

    assert "only a sender" in str(refusal.value)


def test_retracting_twice_says_when_the_first_one_happened(store):
    sent = store.append("tell", SENDER, READER, "flash board 4471")
    store.retract(sent.seq, SENDER)

    with pytest.raises(UsageError) as refusal:
        store.retract(sent.seq, SENDER)

    assert "already withdrawn" in str(refusal.value)


def test_the_body_is_kept_where_a_deleted_note_loses_it(store):
    """Retracting is about delivery that should not happen; deleting a note is about text that should not exist.

    So the sender keeps a record of what it pulled back, and `cairn sent` is where
    that record lives.
    """
    sent = store.append("tell", SENDER, READER, "flash board 4471 tonight")
    store.retract(sent.seq, SENDER)

    (logged,), _ = store.sent(SENDER)

    assert logged.body == "flash board 4471 tonight"
    assert logged.retracted


def test_the_sent_log_shouts_it_because_no_other_surface_shows_it(store):
    """Filtered out of every inbox, so an unmarked row here would read as delivered."""
    sent = store.append("tell", SENDER, READER, "flash board 4471")
    store.retract(sent.seq, SENDER)
    (logged,), total = store.sent(SENDER)

    text = render.sent_text([SentEntry(message=logged, provenance=assess_sent(logged))], total)

    assert "WITHDRAWN" in text
    assert "anybody whose cursor had already passed it still has it" in text


# -- pruning -----------------------------------------------------------------------


def test_pruning_takes_old_read_traffic_and_leaves_what_is_still_waiting(store):
    """The whole command in one assertion, and the second half is the safety property."""
    read = store.append("tell", SENDER, READER, "old and read")
    store.ack(READER, read.seq)
    waiting = store.append("tell", SENDER, READER, "old and unread")
    _age(store)

    removed, kept, kept_by = store.prune(30)

    assert (removed, kept, kept_by) == (1, 1, (READER,))
    assert [m.body for m in store.unread(READER).messages] == ["old and unread"]
    assert waiting.seq > read.seq, "the fixture acked the message it meant to leave behind"


def test_a_peer_that_has_been_switched_off_for_a_week_still_gets_its_backlog(store):
    """The premise of the product, stated as the thing a cleanup may not break."""
    for n in range(5):
        store.append("tell", SENDER, READER, f"message {n}")
    _age(store)

    removed, kept, kept_by = store.prune(1)

    assert (removed, kept, kept_by) == (0, 5, (READER,))
    assert len(store.unread(READER).messages) == 5


def test_recent_traffic_is_never_touched_however_thoroughly_it_was_read(store):
    """Age is a condition, not a consequence of being read."""
    sent = store.append("tell", SENDER, READER, "read five minutes ago")
    store.ack(READER, sent.seq)

    assert store.prune(1) == (0, 0, ())
    assert store.sent(SENDER)[1] == 1


def test_a_retracted_message_is_prunable_even_with_a_cursor_below_it(store):
    """Found live, against this method's own docstring.

    Nobody can read a withdrawn message, so no cursor is waiting on it — but a
    cursor sitting below its seq looks exactly like one that is, and without the
    liveness clause *inside* the hold, the one class of message guaranteed safe to
    prune was the class that never got pruned.
    """
    pulled = store.append("tell", SENDER, READER, "withdrawn and still sitting there")
    store.retract(pulled.seq, SENDER)
    _age(store)

    assert store.prune(30) == (1, 0, ())


def test_pruning_refuses_a_window_that_reaches_today(store):
    """There is no safe way to prune the traffic of the shift that is running."""
    with pytest.raises(UsageError):
        store.prune(0)


def test_notes_are_not_traffic_and_are_never_pruned(store):
    """Messages are a pipe; notes are what outlive a session. The line is the whole point."""
    store.create_subject("rig-a", "thermal chamber A", SENDER)
    store.write_note(SENDER, "clamp is loose", subject="rig-a")
    store._db.execute("UPDATE notes SET created_at = ?", (LONG_AGO,))
    _age(store)

    store.prune(1)

    assert store.notes("rig-a")[1] == 1


# -- the command surface -------------------------------------------------------------


def test_the_command_reports_the_mailboxes_it_spared(hub, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    sent = hub.send("tell", SENDER, READER, "flash board 4471 tonight")

    assert cli.run(["--hub", hub.base_url, "retract", str(sent.seq)]) == 0

    assert "withdrew seq 1 from 1 mailbox" in capsys.readouterr().out
    assert hub.inbox(READER).messages == ()


def test_a_late_retraction_is_exit_three_and_names_the_reader(hub, monkeypatch, capsys):
    """3 is "cannot be carried out as asked". Not 1, which would read as "nothing to report"."""
    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    sent = hub.send("tell", SENDER, READER, "flash board 4471 tonight")
    hub.ack(READER, sent.seq)

    assert cli.run(["--hub", hub.base_url, "retract", str(sent.seq)]) == 3
    assert READER in capsys.readouterr().err


def test_a_broadcast_names_who_it_was_too_late_for(hub, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    sent = hub.send("tell", SENDER, "*", "all hands")
    hub.ack(READER, sent.seq)

    assert cli.run(["--hub", hub.base_url, "retract", str(sent.seq)]) == 0
    printed = capsys.readouterr().out

    assert "withdrew seq 1 from 1 mailbox" in printed
    assert f"too late for {READER}" in printed


def test_pruning_nothing_is_an_answer_rather_than_a_failure(hub, monkeypatch, capsys):
    """Exit 1, on the same rule as an empty inbox: asked, nothing to report."""
    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    hub.send("tell", SENDER, READER, "recent")

    assert cli.run(["--hub", hub.base_url, "prune", "--older-than", "30"]) == 1
    assert "pruned 0 messages" in capsys.readouterr().out


def test_a_retraction_names_the_mailboxes_it_spared_as_well_as_the_ones_it_missed(store, capsys, monkeypatch):
    """From cut 13's acceptance run. Naming only the failures looks like an answer and is not one.

    The sender's next act is deciding who still has to be caught. With the count
    alone, a live session recovered that list by subtracting the named failures
    from a `cairn peers` snapshot it had taken moments earlier — and the snapshot
    was already stale, because a fourth peer had registered between the send and
    the retraction. The names are computed inside `retract`; they were being
    thrown away.
    """
    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    shout = store.append("tell", SENDER, "*", "flash rev C for the 85C segment")
    store.ack(READER, shout.seq)

    withdrawal = store.retract(shout.seq, SENDER)

    assert (withdrawal.withheld, withdrawal.withheld_from, withdrawal.read_by) == (1, (THIRD,), (READER,))


def test_the_command_prints_both_lists_end_to_end(hub, monkeypatch, capsys):
    """Through `cli.run` against a real hub, because the names cross the wire to get here."""
    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    shout = hub.send("tell", SENDER, "*", "flash rev C for the 85C segment")
    hub.ack(READER, shout.seq)

    assert cli.run(["--hub", hub.base_url, "retract", str(shout.seq)]) == 0
    printed = capsys.readouterr().out

    assert f"withheld from {THIRD}" in printed
    assert f"too late for {READER}" in printed


def test_an_older_hub_that_names_nobody_falls_back_to_the_count(monkeypatch, capsys):
    """Half a list is worse than a count, so an absent field prints no list at all.

    `withheld` stays the number of record for this reason. A caller that derived
    the count from the names would report nothing spared against a hub that spared
    several — this project's recurring failure, where the absence of an additive
    field is read as a fact rather than as silence. Item 14 hit the same shape on
    `supersedes` and had the client refuse; here there is nothing to refuse,
    because the retraction genuinely happened and only its account is thinner.
    """
    from cairn.wire import Message, Withdrawal

    old = Withdrawal(message=Message(seq=7, kind="tell", sender=SENDER, recipient="*", body="x"), withheld=2)
    monkeypatch.setenv("CAIRN_AGENT", SENDER)

    def hub_that_does_not_name_them(self, seq, sender):
        """Stand in for a `/v1/retract` built before `withheld_from` existed."""
        return old

    monkeypatch.setattr("cairn.client.HubClient.retract", hub_that_does_not_name_them)

    assert cli.run(["--hub", "http://127.0.0.1:1", "retract", "7"]) == 0
    printed = capsys.readouterr().out

    assert "withdrew seq 7 from 2 mailboxes" in printed
    assert "withheld from" not in printed, "an empty list must not read as 'nobody was spared'"


def test_pruning_names_whose_backlog_it_kept(store):
    """The line the operator is asked about used to end in an indefinite pronoun.

    The instruction this command answers is always about a particular machine
    coming back off leave, and an indefinite pronoun does not say whether that is
    the machine. An acceptance session preserved a backlog, ran the command
    correctly, and had to report that it could confirm *a* backlog and not *the*
    one.
    """
    read = store.append("tell", SENDER, READER, "old and read")
    store.ack(READER, read.seq)
    store.append("tell", SENDER, THIRD, "old and never collected")
    _age(store)

    removed, kept, kept_by = store.prune(30)

    assert (removed, kept, kept_by) == (1, 1, (THIRD,))


def test_the_names_and_the_count_come_from_one_predicate(store):
    """They are asked in opposite directions, so two spellings would drift.

    The drift shows up as a prune that keeps mail while naming nobody — the count
    and the names disagreeing about the very thing the count exists for.
    """
    for reader in (READER, THIRD):
        store.append("tell", SENDER, reader, "never collected")
    _age(store)

    removed, kept, kept_by = store.prune(30)

    assert removed == 0
    assert kept == 2
    assert set(kept_by) == {READER, THIRD}
