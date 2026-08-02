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

import pytest

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
