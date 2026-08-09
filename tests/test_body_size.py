"""How big a body may be, and — the part that actually bit — where that is decided.

The limit itself is uninteresting. What this file is about is a direction that
was backwards for the whole life of the project: **the strictest guard in the
system ran on the code path that reads rows which are already durable.**

`MAX_BODY_CHARS` lived only inside `Message.from_json`. `hub._send` reaches
`store.append` without parsing, so an oversized body was written, answered `200`,
and refused from then on by every reader of that page — the innocent messages
beside it included, since a page is parsed as a unit, and the sender's own
`cairn sent` too, since it parses the same shape. `cairn bell` reported the
mailbox as empty throughout. The sender saw exit **2** and *"hub spoke something
unexpected"*: cairn's "the hub is broken", under a hub that was fine and a
message that had already been delivered. Two such rows are in this fleet's hub.

`store.append`'s docstring already recorded this failure in full — for `kind`,
found and fixed one field at a time. So the general form is what is asserted
here, in `test_whatever_the_store_accepts_can_be_read_back`, which names no
field and is the only test in this file that will still be doing useful work
after somebody adds the next guard.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from cairn import cli, render
from cairn.client import HubClient
from cairn.errors import Unreachable, Unreadable, UsageError
from cairn.hub import make_server
from cairn.provenance import assess_sent
from cairn.store import SqliteStore
from cairn.wire import MAX_BODY_CHARS, Agent, InboxEntry, Message, Note, SentEntry

SENDER = "bench/firmware"
READER = "compute/analysis"
PILE = "rig-a"


@pytest.fixture
def store() -> SqliteStore:
    db = SqliteStore(":memory:")
    for name in (SENDER, READER):
        db.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    db.create_subject(PILE, "opened so a note has somewhere to go", SENDER)
    return db


@pytest.fixture
def hub_store() -> SqliteStore:
    """Return the store behind `hub_server`, exposed so a test can write beneath its guards."""
    return SqliteStore(":memory:")


@pytest.fixture
def hub_server(hub_store: SqliteStore) -> Iterator[ThreadingHTTPServer]:
    server = make_server(hub_store, host="127.0.0.1", port=0)
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
    for name in (SENDER, READER):
        client.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    return client


# -- the direction, which is the whole finding --------------------------------


def test_whatever_the_store_accepts_can_be_read_back(store):
    """The invariant that survives the next guard somebody adds, because it names none.

    Every check in a `from_json` without a counterpart in `store` is a poisoned
    mailbox waiting for an input, and the list of such checks is not fixed — it
    grew by one in cut 4 and by one again in the cut this file comes from. So
    this asserts the *direction* rather than any one rule: push the extremes past
    the store, and require that a reader can still parse what came back.

    A failure here does not mean this test is out of date. It means a guard was
    added to a parser and not to the store, and the mailbox it will brick is a
    real one.
    """
    accepted = [
        store.append("tell", SENDER, READER, "x" * MAX_BODY_CHARS),
        store.append("tell", SENDER, READER, ""),
        store.append("ask", SENDER, READER, "line\nline", correlation_id="q-1"),
    ]
    accepted_notes = [
        store.write_note(SENDER, "y" * MAX_BODY_CHARS, subject=PILE),
        store.write_note(SENDER, "short", subject=PILE),
    ]

    for message in accepted:
        assert Message.from_json(message.to_json()) == message, "the store holds a message its own reader refuses"
    for note in accepted_notes:
        assert Note.from_json(note.to_json()) == note, "the store holds a note its own reader refuses"


def test_one_huge_row_does_not_cost_the_reader_the_page(store):
    """The symptom, asserted from the recipient's side rather than from the parser's.

    Before this cut the oversized row took the whole page with it, so the small
    ordinary message that arrived *first* became unreachable — mail nobody could
    get out, with no seq printed to aim an `ack` past. That is the case to keep
    pinned, because every future version of this bug looks like it from here.
    """
    store.append("tell", SENDER, READER, "the ordinary message that arrived first")
    huge = Message.from_json({**store.append("tell", SENDER, READER, "x").to_json(), "body": "x" * 90_000})
    page = render.inbox_text(
        [InboxEntry(message=m, provenance=assess_sent(m)) for m in (store.unread(READER).messages[0], huge)],
        1,
        "http://hub",
    )

    assert "the ordinary message that arrived first" in page, "one oversized row took an innocent one with it"
    assert "TRUNCATED" in page
    assert "90000" in page, "the marker did not say how much the reader is missing"


def test_a_body_at_the_limit_is_not_truncated(store):
    """The boundary, because a safety net that fires on legitimate traffic is a second bug."""
    exact = store.append("tell", SENDER, READER, "z" * MAX_BODY_CHARS)
    page = render.sent_text([SentEntry(message=exact, provenance=assess_sent(exact))], 1)

    assert "TRUNCATED" not in page


# -- admission, checked at both entries ---------------------------------------


def test_the_store_refuses_before_it_writes(store):
    """`hub._send` stored first and failed while serialising the reply, so a refused send arrived anyway.

    The assertion that matters is the second one. A refusal that leaves the row
    in the recipient's mailbox is worse than no refusal at all: the sender is
    told it failed, so nobody retracts the thing that is now sitting there.
    """
    before = store._head()

    with pytest.raises(UsageError, match="nothing was stored"):
        store.append("tell", SENDER, READER, "x" * (MAX_BODY_CHARS + 1))

    assert store._head() == before, "a refused send was stored anyway"


def test_an_oversized_send_is_exit_3_through_the_cli(hub, monkeypatch):
    """Exit 3, and proven through `cli.run` because that is the only place the code is real.

    `CLAUDE.md` requires this shape for any new validator: the helper returning a
    `UsageError` proves nothing about what a script sees. What a script saw
    before was **2**, cairn's "hub unreachable" — and `errors.py` argues 2 and 3
    cannot collapse precisely because an outage may clear and a malformed
    argument never will. The sender was being told to retry the one thing that
    can only fail again.
    """
    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    argv = ["--hub", hub.base_url, "tell", READER, "x" * (MAX_BODY_CHARS + 1)]

    assert cli.run(argv) == 3
    assert cli.run(["--hub", hub.base_url, "tell", READER, "x" * MAX_BODY_CHARS]) == 0, (
        "the limit itself refused; a 3 above would have proven nothing"
    )


def test_the_refusal_says_the_message_was_not_sent(hub, monkeypatch, capsys):
    """The sentence the old failure got wrong, and the one that decides what the sender does next.

    "Something went wrong" leaves retract-or-rewrite open. Under the old
    behaviour the answer was *retract*, and nothing said so.
    """
    monkeypatch.setenv("CAIRN_AGENT", SENDER)

    cli.run(["--hub", hub.base_url, "tell", READER, "x" * (MAX_BODY_CHARS + 1)])

    err = capsys.readouterr().err
    assert "Nothing was sent" in err
    assert "-a HOST:PATH" in err, "the refusal did not name the way out that the skill teaches"


# -- the bell, which reported all of this as an empty mailbox -----------------


def test_the_bell_stays_quiet_when_the_hub_is_merely_down(hub, monkeypatch, capsys):
    """The half the first version of this fix got wrong, caught by the skill's own check.

    An unreachable hub is the **ordinary** case at a turn boundary. The first
    attempt printed to stderr for every exception the bell swallows, so a briefly
    dead hub produced a line on every single turn — item 18's furniture, added by
    the change that was supposed to remove a silence.

    `client._readable` is why the two were indistinguishable: it converts a
    `WireError` into `Unreachable`, so an unparseable page and a dead socket
    arrived here as the same type. `errors.Unreadable` is a subclass of it, which
    keeps exit 2 for every script and gives this one caller the distinction.
    """
    monkeypatch.setenv("CAIRN_AGENT", SENDER)

    assert cli.run(["--hub", "http://127.0.0.1:9", "bell"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert captured.err == "", "a routine outage printed at a turn boundary"


def test_the_bell_still_prints_nothing_but_no_longer_says_nothing(hub, monkeypatch, capsys):
    """Both halves, because dropping either one is a different bug.

    `{}` and exit 0 are the hook contract and must survive: a bell that errors
    degrades the session it is attached to. What must not survive is the total
    silence — `WireError` is a `ValueError` that `client._readable` turns into
    `Unreachable`, so an unreadable page was caught and reported as an empty
    mailbox at every turn boundary, forever, which is §12 item 6's deafness
    reached from inside.
    """

    def _unreadable(*_args, **_kwargs):
        msg = "bad page"
        raise Unreadable(msg)

    monkeypatch.setenv("CAIRN_AGENT", SENDER)
    monkeypatch.setattr("cairn.client.HubClient.inbox", _unreadable)

    assert cli.run(["--hub", hub.base_url, "bell"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "{}", "the bell broke its own contract with the host product"
    assert "bad page" in captured.err, "an unreadable mailbox was still reported as an empty one"


# -- the floor under both of those, which nothing was standing on -------------


def test_a_page_this_build_cannot_read_arrives_as_its_own_type(hub, hub_store):
    """The conversion the two tests above are built on, and neither of them reaches.

    One points at a dead port; the other monkeypatches `HubClient.inbox` to raise
    `Unreadable` already made. So the line that actually decides an unparseable
    page is not an ordinary outage — `client._readable` — was asserted by
    nothing, and putting `Unreachable` back there leaves the whole suite green
    while the bell goes silent again on precisely the mailbox §12 item 26 is
    about. Measured that way round before this test was written: reverted, 711
    passed.

    That is the second time in this item's history that a live run saw what the
    tests could not, which is the argument for the test being at this level
    rather than one layer up.

    **The row is written beneath `store.append`'s guards deliberately, and that
    is not a fight with `test_whatever_the_store_accepts_can_be_read_back`.**
    That test says no build may *create* such a row from here on. This one says
    the rows older builds already created — two are in this fleet's hub — must
    still arrive as a verdict rather than as silence. Both are needed: closing
    the entrance does nothing about what is already durable behind it.
    """
    hub_store._db.execute(
        "INSERT INTO messages (kind, sender, recipient, body, correlation_id, artifacts, created_at)"
        " VALUES ('shout', ?, ?, 'durable, and unreadable', NULL, '[]', '2026-08-07T09:00:00Z')",
        (SENDER, READER),
    )

    with pytest.raises(Unreadable) as caught:
        hub.inbox(READER)

    assert isinstance(caught.value, Unreachable), (
        "the subclass is what keeps exit 2 for every script and every `except Unreachable` already written"
    )
    assert caught.value.exit_code == 2
