"""Whether a name still means what it meant.

A name is an address, and re-registering one is how a restarted session gets its
mail back — so on the wire, "the same session came back" and "something else took
the name" look identical. That ambiguity was reproduced against a live hub: a
second session claimed an existing name from another directory on another
machine, inherited the cursor, read mail addressed to its predecessor, and
replaced it in `peers`. Neither end was told.

Two halves close it, and they fail in opposite directions on purpose. The hub
stops a newcomer *inheriting* unread mail. The sender stops *new* mail going to
whoever holds the name now. Neither prevents the takeover itself — see I3.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cairn.client import HubClient
from cairn.config import check_pin, forget_pin, pin_of
from cairn.errors import NameMoved, UsageError
from cairn.store import SqliteStore
from cairn.wire import Agent

OLD = "2000-01-01T00:00:00Z"


@pytest.fixture
def store():
    db = SqliteStore(":memory:")
    yield db
    db.close()


def _agent(name: str, machine: str = "bench", cwd: str = "/w/fw") -> Agent:
    return Agent(name=name, machine=machine, cwd=cwd, capabilities=("hil",))


def _cursor(store: SqliteStore, name: str) -> int:
    row = store._db.execute("SELECT last_acked_seq FROM cursors WHERE agent = ?", (name,)).fetchone()
    return int(row["last_acked_seq"])


def _age(store: SqliteStore, name: str) -> None:
    store._db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (OLD, name))


def _last_seen(store: SqliteStore, name: str) -> str:
    return str(store.get_agent(name).last_seen)


# -- the cursor, across the three registration cases ---------------------------


def test_a_new_name_starts_at_the_head(store):
    """A fresh session is not buried under a month of other people's mail."""
    store.register(_agent("sender"))
    store.register(_agent("old-hand", cwd="/w/other"))
    for _ in range(3):
        store.append("tell", "sender", "old-hand", "backlog")
    store.register(_agent("newcomer", cwd="/w/new"))
    assert store.unread("newcomer").messages == ()


def test_a_returning_session_keeps_its_backlog(store):
    """Same name, same place: a restart still gets what it missed."""
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("bench/firmware"))
    store.append("tell", "sender", "bench/firmware", "sent while it was down")
    store.register(_agent("bench/firmware"))
    assert [m.body for m in store.unread("bench/firmware").messages] == ["sent while it was down"]


def test_a_takeover_does_not_inherit_the_predecessors_mail(store):
    """The bug this file exists for, at the smallest scale that shows it."""
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("bench/firmware", machine="bench", cwd="/w/fw"))
    store.append("tell", "sender", "bench/firmware", "the flash key is in the usual place")
    store.register(_agent("bench/firmware", machine="some-other-box", cwd="/w/elsewhere"))
    assert store.unread("bench/firmware").messages == ()


@pytest.mark.parametrize(
    ("machine", "cwd"),
    [("some-other-box", "/w/fw"), ("bench", "/w/elsewhere")],
)
def test_either_half_of_the_pair_moving_counts_as_a_takeover(store, machine, cwd):
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("held"))
    store.append("tell", "sender", "held", "mail")
    store.register(_agent("held", machine=machine, cwd=cwd))
    assert store.unread("held").messages == ()


def test_a_takeover_is_dated_from_its_own_arrival(store):
    """Keeping the old date would age the newcomer to its predecessor."""
    first = store.register(_agent("held"))
    store.register(_agent("sender", cwd="/w/send"))
    store.append("tell", "sender", "held", "mail")
    store._db.execute("UPDATE agents SET registered_at = ? WHERE name = ?", (OLD, "held"))
    second = store.register(_agent("held", machine="elsewhere", cwd="/w/elsewhere"))
    assert second.agent.registered_at != OLD
    assert first.agent.registered_at != OLD


def test_the_cursor_only_ever_moves_forward(store):
    """A takeover jumps the cursor to the head; it must never rewind one."""
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("held"))
    for _ in range(4):
        store.append("tell", "sender", "held", "mail")
    store.ack("held", 4)
    store.register(_agent("held", machine="elsewhere", cwd="/w/elsewhere"))
    assert _cursor(store, "held") == 4


# -- last_seen means what it says ----------------------------------------------


def test_last_seen_moves_when_an_agent_sends(store):
    """It used to mean `last_registered`: a peer eight messages deep still advertised joining."""
    store.register(_agent("sender"))
    store.register(_agent("recipient", cwd="/w/rx"))
    _age(store, "sender")
    store.append("tell", "sender", "recipient", "hello")
    assert _last_seen(store, "sender") != OLD


def test_last_seen_moves_when_an_agent_reads(store):
    store.register(_agent("reader"))
    _age(store, "reader")
    store.unread("reader")
    assert _last_seen(store, "reader") != OLD


def test_last_seen_moves_when_an_agent_acks(store):
    store.register(_agent("reader"))
    _age(store, "reader")
    store.ack("reader", 1)
    assert _last_seen(store, "reader") != OLD


def test_touching_an_unregistered_name_is_harmless(store):
    """`ack` must record a cursor even for a name with no agent row."""
    assert store.ack("never-registered", 3) == 3


# -- what the store will let through the door ----------------------------------


def test_a_kind_the_wire_would_reject_is_refused_at_the_door(store):
    """A kind the reader cannot decode must never become durable.

    `hub._send` hands `obj.get("kind", "tell")` straight to `append`, and
    `Message.from_json` rejects unknown kinds — so a stored `"shout"` raised
    `WireError` on every later read of that mailbox. That is a `ValueError`,
    which `run()` does not catch, so the reader got a traceback and exit 1:
    a poisoned inbox indistinguishable from an empty one, forever.
    """
    store.register(_agent("sender"))
    store.register(_agent("recipient", cwd="/w/rx"))
    with pytest.raises(UsageError) as caught:
        store.append("shout", "sender", "recipient", "not a kind")
    assert "shout" in str(caught.value)
    for kind in ("tell", "ask", "reply"):
        assert store.append(kind, "sender", "recipient", "fine").kind == kind


# -- saying what the takeover did, and undoing it ------------------------------


def test_a_new_name_has_nothing_to_report(store):
    assert store.register(_agent("fresh")).arrival == "new"


def test_a_returning_session_is_reported_as_such(store):
    store.register(_agent("held"))
    assert store.register(_agent("held")).arrival == "returning"


def test_a_takeover_reports_what_it_skipped_and_where_to_resume(store):
    """A cursor moved and mail became unreachable. Saying so is the whole fix."""
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("held", machine="bench", cwd="/w/fw"))
    for _ in range(5):
        store.append("tell", "sender", "held", "mail")
    taken = store.register(_agent("held", machine="some-other-box", cwd="/w/elsewhere"))
    assert taken.arrival == "takeover"
    assert taken.skipped == 5
    assert taken.previous == "bench:/w/fw"
    assert taken.resume_at == 0


def test_the_resume_point_is_the_cursor_as_it_stood_not_zero(store):
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("held"))
    for _ in range(5):
        store.append("tell", "sender", "held", "mail")
    store.ack("held", 2)
    taken = store.register(_agent("held", machine="elsewhere", cwd="/w/elsewhere"))
    assert (taken.skipped, taken.resume_at) == (3, 2)


def test_rewinding_to_the_reported_point_restores_exactly_what_was_skipped(store):
    """Without this the loss is unrecoverable: the mail is in the table, out of reach."""
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("held"))
    for i in range(5):
        store.append("tell", "sender", "held", f"msg {i + 1}")
    store.ack("held", 2)
    taken = store.register(_agent("held", machine="elsewhere", cwd="/w/elsewhere"))
    assert store.unread("held").messages == ()
    store.ack("held", taken.resume_at, rewind=True)
    assert [m.body for m in store.unread("held").messages] == ["msg 3", "msg 4", "msg 5"]


def test_an_ordinary_ack_still_refuses_to_rewind(store):
    """Forward-only is about out-of-order acks, and that reason has not gone away."""
    store.register(_agent("reader"))
    store.ack("reader", 5)
    assert store.ack("reader", 2) == 5


# -- the sending side ----------------------------------------------------------


@pytest.fixture
def pins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def live_hub():
    """Yield the base URL of a hub on a real loopback socket.

    The exit codes in this file are the interface (`cli.py`'s module docstring),
    and the one way they have escaped is a validator proved by calling the helper
    instead of `cli.run` — three separate times in one cut. So every refusal here
    is asserted through `cli.run` against a real hub, which means the whole path:
    store guard, 400, `client._call`, `CairnError`, exit code.
    """
    import threading

    from cairn.hub import make_server

    server = make_server(SqliteStore(":memory:"), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.notifier.close_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_pin_is_recorded_on_first_use_and_then_agrees(pins):
    check_pin("bench/firmware", "bench", "/w/fw")
    check_pin("bench/firmware", "bench", "/w/fw")


def test_a_moved_name_refuses_the_send(pins):
    check_pin("bench/firmware", "bench", "/w/fw")
    with pytest.raises(NameMoved) as caught:
        check_pin("bench/firmware", "some-other-box", "/w/elsewhere")
    assert "Nothing was sent" in str(caught.value)


def test_the_refusal_says_what_the_name_used_to_reach(pins):
    """A refusal that does not name the old holder leaves you no way to judge it."""
    check_pin("bench/firmware", "bench", "/w/fw")
    with pytest.raises(NameMoved) as caught:
        check_pin("bench/firmware", "elsewhere", "/w/new")
    assert pin_of("bench", "/w/fw") in str(caught.value)


def test_forget_lets_the_move_through(pins):
    check_pin("bench/firmware", "bench", "/w/fw")
    assert forget_pin("bench/firmware") is True
    check_pin("bench/firmware", "elsewhere", "/w/new")


def test_forgetting_a_name_that_was_never_pinned_says_so(pins):
    assert forget_pin("never-sent-to") is False


def test_pins_are_per_sending_directory(pins, monkeypatch):
    """One directory's history should not veto another's first contact."""
    check_pin("bench/firmware", "bench", "/w/fw")
    other = pins / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    check_pin("bench/firmware", "some-other-box", "/w/new")


def test_pinning_one_name_does_not_pin_another(pins):
    check_pin("a", "m1", "/w/1")
    check_pin("b", "m2", "/w/2")
    with pytest.raises(NameMoved):
        check_pin("a", "m2", "/w/2")


def test_an_unreadable_pin_file_fails_open_rather_than_blocking_all_sends(pins):
    """A corrupt pin file must not wedge every send from this directory."""
    from cairn.config import _pin_file

    path = _pin_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    check_pin("bench/firmware", "bench", "/w/fw")


# -- renaming a directory's identity -------------------------------------------


def test_registering_a_new_name_says_what_the_directory_is_leaving_behind(pins, live_hub, capsys):
    """From cut 13's acceptance run, and the near-miss it recorded was the dangerous kind.

    The pin is per-directory, so a fresh session inherits the previous one's
    identity — and with it the sent log, the read cursor, and the only right to
    withdraw its unread mail, because `retract` refuses anybody but the sender.
    The skill's own advice is to register on arrival; the obvious name is the last
    one with a suffix. A live session was one command from doing exactly that
    while a broadcast telling two machines to flash a withdrawn board sat unread
    on the hub, and reported afterwards that what stopped it was its operator's
    instruction to establish state first, not anything cairn said.
    """
    from cairn import cli

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift"]) == 0
    capsys.readouterr()

    assert cli.run(["--hub", base, "register", "hil-a/dayshift-2"]) == 0
    printed = capsys.readouterr().out

    assert "left behind  hil-a/dayshift" in printed
    assert "cairn retract" in printed, "the right that is actually lost has to be the one named"
    assert "cairn register hil-a/dayshift" in printed, "and the way back, since there is one"


def test_re_registering_the_same_name_says_nothing_about_leaving_anything(pins, live_hub, capsys):
    """Registering again under the same name is the harmless case, and a line there would train the other one past."""
    from cairn import cli

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift"]) == 0
    capsys.readouterr()

    assert cli.run(["--hub", base, "register", "hil-a/dayshift"]) == 0
    printed = capsys.readouterr().out

    assert "left behind" not in printed


def test_a_returning_registration_says_what_happened_to_its_capabilities(pins, live_hub, capsys):
    """Registering is the only way to edit the list, and it used to wipe it in silence.

    The upsert replaces `capabilities` wholesale, so a session that re-registers
    and forgets `-c` clears everything it was advertising. The cost of that lands
    on somebody else — `cairn peers -c hil` is how a peer finds the machine with
    the hardware — and until this line neither end was told anything had changed.
    """
    from cairn import cli

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift", "-c", "hil", "-c", "jtag"]) == 0
    capsys.readouterr()

    assert cli.run(["--hub", base, "register", "hil-a/dayshift", "-c", "hil", "-c", "swd"]) == 0
    printed = capsys.readouterr().out

    assert "+swd" in printed
    assert "-jtag" in printed
    assert "+hil" not in printed, "a capability that did not move is not a change"


def test_dropping_every_capability_says_so_in_words(pins, live_hub, capsys):
    """The silent wipe is the whole reason the diff exists, so the empty case gets a sentence."""
    from cairn import cli

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift", "-c", "hil"]) == 0
    capsys.readouterr()

    assert cli.run(["--hub", base, "register", "hil-a/dayshift"]) == 0
    printed = capsys.readouterr().out

    assert "-hil" in printed
    assert "replaces the list" in printed


def test_a_first_registration_reports_no_capability_change(pins, live_hub, capsys):
    """An older hub sends no previous list, which is the same bytes as "it had none".

    So a new name arriving with capabilities must not be rendered as a change.
    Getting this wrong would print a diff at every first registration, which is
    furniture — and worse, would make the real wipe indistinguishable from noise.
    """
    from cairn import cli

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift", "-c", "hil"]) == 0
    printed = capsys.readouterr().out

    assert "changed" not in printed


# -- moving a name, and taking one off the hub ---------------------------------


def test_a_rename_carries_the_cursor(store):
    """The whole reason this exists rather than registering under the new name.

    A new registration parks at the head, so anything the session had not read
    yet stops being reachable. A rename is the same session with a different
    address, so the read position is the one thing that must not move.
    """
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("holder"))
    for _ in range(4):
        store.append("tell", "sender", "holder", "mail")
    store.ack("holder", 4)

    moved = store.rename("holder", "holder-2", machine="bench", cwd="/w/fw")

    assert moved.agent.name == "holder-2"
    assert moved.acked == 4
    assert _cursor(store, "holder-2") == 4
    assert store.get_agent("holder") is None


def test_a_rename_leaves_the_sent_history_under_the_old_name(store):
    """`signing.canonical` covers `sender`, so rewriting it would forge nothing and break everything.

    The count is reported rather than the rows being moved, because the caller's
    real question is why `cairn sent` is empty under the new name.
    """
    store.register(_agent("holder"))
    store.register(_agent("peer", cwd="/w/peer"))
    store.append("tell", "holder", "peer", "before the rename")

    moved = store.rename("holder", "holder-2", machine="bench", cwd="/w/fw")

    assert moved.sent_kept == 1
    assert store.sent("holder-2")[1] == 0
    assert store.sent("holder")[1] == 1


def test_a_signature_written_before_a_rename_still_verifies_after_it(store, tmp_path, monkeypatch):
    """The property the whole design is built on, proven rather than asserted.

    If a rename rewrote `messages.sender` — the obvious "tidy" version of this
    command — every message the session had ever signed would read MISMATCH on
    the machine that receives it, and a reader has no way to tell bookkeeping
    from forgery. So the test is not "sender is unchanged", which is a statement
    about a column; it is that the check a peer actually runs still passes.
    """
    from cairn import signing

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store.register(_agent("holder"))
    store.register(_agent("peer", cwd="/w/peer"))
    sent = store.append("tell", "holder", "peer", "signed under the old name")
    signature = signing.sign(sent)
    signed = replace(sent, signature=signature)
    assert signing.verify(signed)

    store.rename("holder", "holder-2", machine="bench", cwd="/w/fw")

    assert signing.verify(signed), "a rename that broke this would look exactly like a forgery"


def test_a_rename_refuses_a_name_somebody_holds(store):
    store.register(_agent("holder"))
    store.register(_agent("taken", cwd="/w/other"))
    with pytest.raises(UsageError, match="already registered"):
        store.rename("holder", "taken", machine="bench", cwd="/w/fw")


def test_a_rename_refuses_from_anywhere_but_where_the_name_lives(store):
    """A rename carries the cursor, so from elsewhere it would be a takeover that inherits the backlog.

    That is the exact thing `register` was fixed to stop, and it would arrive
    through a door with a friendlier name. The refusal points at `deregister`,
    because wanting to clear a name from another machine is a real thing to want.
    """
    store.register(_agent("holder"))
    with pytest.raises(UsageError, match="only the holder") as caught:
        store.rename("holder", "holder-2", machine="elsewhere", cwd="/w/elsewhere")
    assert "cairn deregister holder" in str(caught.value)


def test_a_rename_refuses_while_mail_is_unread(store):
    """Those rows are addressed to the old name; moving it puts them out of reach of every command."""
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("holder"))
    store.append("tell", "sender", "holder", "unread")

    with pytest.raises(UsageError, match="reachable by nothing"):
        store.rename("holder", "holder-2", machine="bench", cwd="/w/fw")

    assert store.get_agent("holder") is not None, "a refusal must not half-do it"


def test_a_rename_asked_for_anyway_says_how_much_it_stranded(store):
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("holder"))
    for _ in range(3):
        store.append("tell", "sender", "holder", "unread")

    moved = store.rename("holder", "holder-2", machine="bench", cwd="/w/fw", anyway=True)

    assert moved.stranded == 3
    assert store.unread("holder-2").unread == 0, "stranded means stranded; the new name cannot see them"


def test_a_rename_to_the_same_name_is_refused_rather_than_silently_nothing(store):
    store.register(_agent("holder"))
    with pytest.raises(UsageError, match="already this directory's name"):
        store.rename("holder", "holder", machine="bench", cwd="/w/fw")


def test_deregistering_takes_the_row_and_the_cursor(store):
    store.register(_agent("holder"))
    store.ack("holder", 0)

    gone = store.deregister("holder")

    assert gone.name == "holder"
    assert store.get_agent("holder") is None
    assert store.peers() == []
    assert store._db.execute("SELECT COUNT(*) AS c FROM cursors WHERE agent = 'holder'").fetchone()["c"] == 0


def test_deregistering_names_the_registration_that_replaced_it_in_place(store):
    """The one piece of evidence that a row is a corpse rather than a quiet session.

    This is the shape the command was built for: a peer renamed by registering a
    new name in the same directory, and both rows then sat in `peers` on the same
    machine and the same cwd. `last_seen` cannot separate a corpse from a session
    that has not spoken today; a second registration on the same `(machine, cwd)`
    can.
    """
    store.register(_agent("old-name"))
    store.register(_agent("new-name"))

    gone = store.deregister("old-name")

    assert gone.superseded_by == "new-name"


def test_deregistering_a_name_nothing_replaced_says_so_by_saying_nothing(store):
    store.register(_agent("holder"))
    assert store.deregister("holder").superseded_by == ""


def test_deregistering_refuses_while_mail_is_unread(store):
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("holder"))
    store.append("tell", "sender", "holder", "unread")

    with pytest.raises(UsageError, match="reachable by nothing"):
        store.deregister("holder")

    assert store.get_agent("holder") is not None


def test_deregistering_anyway_reports_what_it_stranded(store):
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("holder"))
    for _ in range(2):
        store.append("tell", "sender", "holder", "unread")

    gone = store.deregister("holder", anyway=True)

    assert gone.stranded == 2
    assert gone.sent_kept == 0


def test_deregistering_an_unknown_name_lists_who_is_actually_here(store):
    """The same courtesy `append` pays a misaddressed send: the answer is usually a typo."""
    store.register(_agent("holder"))
    with pytest.raises(UsageError, match="holder"):
        store.deregister("holdre")


def test_a_deregistered_name_reads_as_refused_and_not_as_an_empty_inbox(store):
    """The door this command opens, and the reason `unread` now refuses.

    An empty page and "no mail" are the same sentence. A session whose
    registration was removed would have been told, at every turn boundary and for
    ever, that nobody had written to it.
    """
    store.register(_agent("holder"))
    store.deregister("holder")

    with pytest.raises(UsageError, match="no mailbox under that name"):
        store.unread("holder")


def test_mail_to_a_deregistered_name_is_refused_at_the_door(store):
    """`append` already checks the recipient, so removing the name makes sends loud rather than silent."""
    store.register(_agent("sender", cwd="/w/send"))
    store.register(_agent("holder"))
    store.deregister("holder")

    with pytest.raises(UsageError, match="unknown recipient"):
        store.append("tell", "sender", "holder", "still there?")


# -- the same refusals, through `cli.run`, where the exit code is the interface --


def test_rename_and_deregister_refusals_exit_three(pins, live_hub, capsys):
    """Every new validator proved through `cli.run`, because that is how they have escaped before.

    A `WireError` reaching `run()` is a traceback plus exit 1 — the code for
    "asked, nothing to report" — and it got out three separate ways in one cut by
    being proved with a call to the helper instead of through the command line.
    So: the store guard, the 400, `client._call`, the `CairnError`, and the
    number a script reads.
    """
    from cairn import cli

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift"]) == 0
    assert cli.run(["--hub", base, "rename", "hil-a/dayshift"]) == 3, "renaming to the name you have"
    assert cli.run(["--hub", base, "deregister", "hil-a/nobody"]) == 3, "a name nobody holds"
    capsys.readouterr()


def test_renaming_through_the_cli_keeps_the_directory_pointing_at_the_new_name(pins, live_hub, capsys):
    from cairn import cli
    from cairn.config import current_identity

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift"]) == 0
    capsys.readouterr()

    assert cli.run(["--hub", base, "rename", "hil-a/nightshift"]) == 0
    printed = capsys.readouterr().out

    assert current_identity() == "hil-a/nightshift"
    assert "renamed hil-a/dayshift to hil-a/nightshift" in printed
    assert cli.run(["--hub", base, "whoami"]) == 0
    assert capsys.readouterr().out.strip() == "hil-a/nightshift"


def test_deregistering_this_directory_drops_its_identity_file(pins, live_hub, capsys):
    """Leaving it behind points every later command here at a mailbox the hub does not have."""
    from cairn import cli
    from cairn.config import current_identity

    base = live_hub
    assert cli.run(["--hub", base, "register", "hil-a/dayshift"]) == 0
    capsys.readouterr()

    assert cli.run(["--hub", base, "deregister"]) == 0
    printed = capsys.readouterr().out

    assert current_identity() is None
    assert "no longer registered" in printed


def test_deregistering_somebody_else_leaves_this_directorys_identity_alone(pins, live_hub, capsys):
    """The operator case: clearing up after a holder that is gone, from a session that is not it."""
    from cairn import cli
    from cairn.config import current_identity

    base = live_hub
    assert cli.run(["--hub", base, "register", "ops/hub"]) == 0
    HubClient(base).register(Agent(name="hil-a/gone", machine="elsewhere", cwd="/w/gone"))
    capsys.readouterr()

    assert cli.run(["--hub", base, "deregister", "hil-a/gone"]) == 0
    printed = capsys.readouterr().out

    assert current_identity() == "ops/hub"
    assert "no longer registered" not in printed
    assert "removed hil-a/gone" in printed


def test_a_hub_without_the_route_says_it_is_old_rather_than_unreachable(pins, capsys):
    """A 404 used to read as "hub unreachable", which sends the reader to look at the network.

    The production hub this was written for predates the command by one release,
    so this is the first thing an operator would have met.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cairn import cli

    class OldHub(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "no route /v1/rename"}')

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), OldHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://{}:{}".format(*server.server_address[:2])
        from cairn.config import remember_identity

        remember_identity("hil-a/dayshift")
        assert cli.run(["--hub", base, "rename", "hil-a/nightshift"]) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "predates the command" in capsys.readouterr().err
