"""The window: reading past what you have already looked at, without consuming it.

`docs/design.md` §12 item 7's tail is the evidence. A live session with a backlog
larger than one page had `--limit` as its only control, so the way to see further
was to raise it — which re-fetches everything already read. It fetched the same
fifty rows three times and then used `tail -c` as the offset the tool did not
have, cutting one record in half and spending a fourth round trip recovering it.

Three properties carry this feature, and each is something a plausible patch
removes:

- **The window narrows the page and nothing else.** `unread` and `head` are facts
  about the mailbox; recomputing them under a caller's window would make "unread"
  mean whatever the last reader typed, and the bell reads both.
- **A windowed read acknowledges nothing.** Everything between the cursor and the
  floor was not shown, so an ack would step the cursor over it — the one failure
  `cairn inbox` has never had.
- **A hub that does not understand the window is refused, not accommodated.** It
  answers the unwindowed question in the windowed question's shape, which is the
  one cross-version case where falling back shows mail from the wrong end of the
  queue rather than merely losing a number.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from cairn import cli, provenance, render
from cairn.client import HubClient
from cairn.errors import UsageError
from cairn.hub import make_server
from cairn.store import SqliteStore
from cairn.wire import Agent, InboxEntry, InboxPage, Message

READER = "bench/firmware"
WRITER = "compute/analysis"


@pytest.fixture
def store() -> SqliteStore:
    """Return a store with a reader, a writer, and nothing sent yet."""
    db = SqliteStore(":memory:")
    for name in (READER, WRITER):
        db.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    return db


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
    """Return a client pointed at the running hub, with both agents registered."""
    host, port = hub_server.server_address[:2]
    client = HubClient(f"http://{host}:{port}", timeout=5.0)
    for name in (READER, WRITER):
        client.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    return client


def _fill(store: SqliteStore, count: int) -> list[int]:
    return [store.append("tell", WRITER, READER, f"result {n}").seq for n in range(count)]


def _entry(seq: int) -> InboxEntry:
    message = Message(seq=seq, kind="tell", sender=WRITER, recipient=READER, body=f"body {seq}")
    return InboxEntry(message=message, provenance=provenance.assess(message))


# -- the store -----------------------------------------------------------------


def test_the_window_moves_the_floor_of_the_page_and_nothing_else(store):
    """The whole feature in one assertion, plus the two numbers it must not touch."""
    _fill(store, 7)

    page = store.unread(READER, limit=50, since=4)

    assert [m.seq for m in page.messages] == [5, 6, 7]
    assert page.unread == 7, "the window was applied to the backlog count"
    assert page.head == 7, "the window was applied to the head, which is what the bell latches on"
    assert page.matching == 3


def test_paging_with_the_window_never_repeats_a_row(store):
    """The workaround this replaces, run properly: three pages, no row seen twice.

    Raising `--limit` is the only other way through a backlog, and it re-fetches
    from the oldest end every time — which is how a live session came to hold the
    same fifty rows three times and reach for `tail -c`.
    """
    _fill(store, 7)

    seen: list[int] = []
    floor = 0
    while True:
        page = store.unread(READER, limit=3, since=floor)
        if not page.messages:
            break
        seen.extend(m.seq for m in page.messages)
        floor = page.messages[-1].seq

    assert seen == [1, 2, 3, 4, 5, 6, 7]
    assert len(seen) == len(set(seen)), "a row came back on two different pages"


def test_reading_through_the_window_consumes_nothing(store):
    """A look, not a read. The store half of it: no cursor moves anywhere."""
    _fill(store, 5)

    store.unread(READER, limit=50, since=2)
    store.unread(READER, limit=50, since=4)

    assert store.unread(READER).floor == 0
    assert [m.seq for m in store.unread(READER).messages] == [1, 2, 3, 4, 5]


def test_the_floor_is_the_cursor_when_the_window_is_behind_it(store):
    """`max(cursor, since)`, which is what keeps a windowed page free of read mail.

    Widening the window past the cursor is the tempting alternative — it would
    make `--since` a history browser for free. It would also put consumed and
    unconsumed traffic on one page with nothing marking which is which, and the
    thing a reader does with a message it has already acted on is act on it again.
    """
    _fill(store, 5)
    store.ack(READER, 3)

    page = store.unread(READER, limit=50, since=1)

    assert [m.seq for m in page.messages] == [4, 5]
    assert page.floor == 3, "nothing in the response could explain the missing rows"
    assert page.since == 1, "the echo reports what was asked for, not the floor that bit"


def test_a_window_past_the_newest_message_matches_nothing_over_a_full_mailbox(store):
    """Zero matching is an answer, and it is not an empty mailbox."""
    _fill(store, 4)

    page = store.unread(READER, limit=50, since=99)

    assert page.messages == ()
    assert (page.unread, page.matching) == (4, 0)


def test_an_unregistered_name_still_sees_nothing_through_a_window(store):
    """The cursor stays a subselect, and this is what that buys.

    `seq > NULL` is NULL, so a name with no cursor row selects nothing. Binding a
    cursor of zero instead — which the reported `cursor` is — would hand every
    unregistered name every broadcast on the hub.
    """
    store.append("tell", WRITER, "*", "anyone got a spare chamber slot")

    assert store.unread("nobody/here", since=0).messages == ()
    assert store.unread("nobody/here", since=1).messages == ()


# -- the wire shape ------------------------------------------------------------


def test_truncation_is_measured_against_the_window_and_not_the_backlog():
    """Against the backlog it blames `--limit` for rows the window excluded.

    The reader is then told to raise a limit that cannot bring them back, which
    is a wrong instruction printed in the position the truncation line earned by
    being right.
    """
    page = InboxPage(
        messages=tuple(Message(seq=s, kind="tell", sender="a", recipient="b", body="x") for s in (5, 6)),
        unread=40,
        head=40,
        since=4,
        matching=2,
    )

    assert page.available == 2
    assert not page.truncated


def test_a_hub_that_reports_no_window_leaves_matching_unresolved():
    """`None` is "this hub said nothing about a window", which is the signal, not zero."""
    old = {"messages": [Message(seq=4, kind="tell", sender="a", recipient="b", body="hi").to_json()], "unread": 9}

    page = InboxPage.from_json(old)

    assert page.matching is None
    assert page.available == 9, "the fallback is the backlog, which is what an unwindowed read means"
    assert (page.since, page.floor) == (0, 0)


def test_the_window_survives_the_round_trip():
    """What the hub serializes is what the client parses, `matching: null` included."""
    for original in (
        InboxPage(unread=9, head=9, floor=4, since=4, matching=5),
        InboxPage(unread=9, head=9),
    ):
        assert InboxPage.from_json(json.loads(json.dumps(original.to_json()))) == original


# -- the cross-version refusal -------------------------------------------------


def test_a_hub_that_ignores_the_window_is_refused_rather_than_believed(hub, monkeypatch):
    """The one place this cut refuses where the last one fell back, and why.

    A hub built before `?since=` does not withhold a number — it answers the
    whole-backlog question in the windowed question's shape, and the oldest page
    of a backlog printed as "the part you have not read" is mail shown in the
    wrong place. So the hub echoes the window it applied and the client stops when
    the echo does not come back.
    """
    hub.send("tell", WRITER, READER, "the first of many")

    def answer_without_the_window(self, method, path, payload=None, **query):
        return {"messages": [], "unread": 1, "head": 1}

    monkeypatch.setattr(HubClient, "_call", answer_without_the_window)

    with pytest.raises(UsageError) as refusal:
        hub.inbox(READER, since=1)

    assert "does not support --since" in str(refusal.value)
    assert refusal.value.exit_code == 3, "a hub that is up and answering is not an unreachable hub"


def test_an_unwindowed_read_against_the_same_old_hub_still_works(hub, monkeypatch):
    """The refusal is scoped to the flag. Everything else degrades exactly as it did.

    An old hub is a hub that works; it simply cannot do this one thing. Refusing
    it wholesale would break messaging over a parameter the caller did not use.
    """

    def answer_without_the_window(self, method, path, payload=None, **query):
        return {"messages": [Message(seq=1, kind="tell", sender=WRITER, recipient=READER, body="hi").to_json()]}

    monkeypatch.setattr(HubClient, "_call", answer_without_the_window)

    page = hub.inbox(READER)

    assert [m.seq for m in page.messages] == [1]
    assert page.available == 1


# -- the command surface -------------------------------------------------------


def test_a_windowed_read_marks_nothing_read(hub, monkeypatch, capsys):
    """The ack is `max(seq)` of what was printed, and a window prints only part.

    Acking here would step the cursor past everything between the cursor and the
    floor — mail nobody was shown, gone from the inbox, reachable only by
    `--rewind`. So the flag does not acknowledge at all, and says so.
    """
    for n in range(5):
        hub.send("tell", WRITER, READER, f"result {n}")
    monkeypatch.setenv("CAIRN_AGENT", READER)

    assert cli.run(["--hub", hub.base_url, "inbox", "--since", "3"]) == 0
    printed = capsys.readouterr().out

    assert "[1] seq 4" in printed
    assert render.WINDOW_CLAUSE in printed
    assert [m.seq for m in hub.inbox(READER).messages] == [1, 2, 3, 4, 5], "a windowed read moved the cursor"


def test_the_window_and_the_wait_cannot_be_combined(hub, monkeypatch):
    """The waiter docs/design.md §12 item 3 rules out by name, reconstructed from two flags.

    "Watch for anything after my ask" is the plausible one and the one most likely
    to be written. It fails on the exchange that taught the rule: a peer answered
    an *earlier* `tell` seconds before the `ask` landed, so the answer that settled
    the question carried the **lower** seq. A wait floored at a seq blocks straight
    through it — for the whole deadline, with mail sitting unread below the floor.
    """
    monkeypatch.setenv("CAIRN_AGENT", READER)

    assert cli.run(["--hub", hub.base_url, "inbox", "--since", "3", "--wait", "5"]) == 3


def test_a_negative_window_is_refused_at_the_boundary(hub, monkeypatch):
    """Asserted through `cli.run`, because the exit code is the interface.

    A `WireError` or a bare `ValueError` reaching `run()` is a traceback plus exit
    1 — the code for "asked, nothing to report" — which is the poisoned-mailbox
    shape wearing a different hat. A malformed argument is exit 3.
    """
    monkeypatch.setenv("CAIRN_AGENT", READER)

    assert cli.run(["--hub", hub.base_url, "inbox", "--since", "-1"]) == 3


def test_an_empty_window_over_a_full_mailbox_does_not_read_as_an_empty_mailbox(hub, monkeypatch, capsys):
    """Three kinds of nothing, and this is the one the reader would act on unchecked."""
    hub.send("tell", WRITER, READER, "still waiting for you")
    monkeypatch.setenv("CAIRN_AGENT", READER)

    assert cli.run(["--hub", hub.base_url, "inbox", "--since", "99"]) == 1
    printed = capsys.readouterr().out

    assert "1 unread, none of them after seq 99" in printed
    assert "no unread messages" not in printed


# -- the rendering -------------------------------------------------------------


def test_the_header_says_both_numbers_under_a_window():
    """Only the windowed count would report a smaller mailbox than exists."""
    text = render.inbox_text([_entry(51), _entry(52)], total=63, since=50, matching=13)

    assert text.splitlines()[0].startswith("cairn inbox: 63 unread · 13 after seq 50")
    assert "showing the oldest 2 of 13" in text


def test_a_window_the_cursor_has_overtaken_explains_itself(hub, monkeypatch, capsys):
    """The natural thing to try after a takeover, and it silently does nothing.

    `cairn register` prints a seq to resume from; `cairn inbox --since <that seq>`
    is what a reader tries before reading the sentence that says `--rewind`. The
    floor is `max(cursor, since)`, so the answer is correct and unexplained.
    """
    for n in range(4):
        hub.send("tell", WRITER, READER, f"result {n}")
    hub.ack(READER, 3)
    monkeypatch.setenv("CAIRN_AGENT", READER)

    assert cli.run(["--hub", hub.base_url, "inbox", "--since", "1"]) == 0
    printed = capsys.readouterr().out

    assert "--since 1 is behind your read cursor at 3" in printed
    assert "cairn ack 1 --rewind" in printed


def test_the_explanation_survives_an_empty_answer():
    """Where it is needed most and has no page to hang under.

    One sentence framed twice rather than two wordings, because the empty case is
    exactly where a reader is left with nothing to reason from.
    """
    text = render.inbox_text([], total=0, since=1, floor=9)

    assert "no unread messages" in text
    assert "--since 1 is behind your read cursor at 9" in text.splitlines()[1]


def test_an_unwindowed_read_says_nothing_about_windows():
    """A line that appears when it need not is one a reader learns to skip."""
    text = render.inbox_text([_entry(1)], total=1)

    assert "after seq" not in text
    assert "--since" not in text
    assert "read cursor" not in text


def test_the_json_reports_the_backlog_the_window_and_the_page_separately():
    """Three numbers a paging program needs, and the echo that says the window applied."""
    payload = json.loads(render.inbox_json([_entry(51)], total=63, since=50, matching=13, floor=40))

    assert (payload["unread"], payload["matching"], payload["showing"]) == (63, 13, 1)
    assert (payload["since"], payload["floor"]) == (50, 40)


def test_the_json_shape_does_not_vary_with_the_flags():
    """A parser branching on which keys turned up is a parser that will get it wrong."""
    assert list(json.loads(render.inbox_json([], total=0))) == list(
        json.loads(render.inbox_json([_entry(9)], total=9, since=8, matching=1, floor=8))
    )


# -- what the acceptance run found ---------------------------------------------


def test_a_window_at_zero_is_a_question_and_gets_an_answer(hub, monkeypatch, capsys):
    """Found by an independent session, and it put a wrong sentence in a shift summary.

    With its mailbox already drained the session ran `cairn inbox --since 0` to
    find out whether an earlier backlog existed and was its own, got "no unread
    messages", and reported that there was no earlier mail. The command could not
    have answered that — the floor was its own cursor — and the note explaining
    exactly that was suppressed, because a typed `0` was being read as "no window
    asked for".

    `None` is no window. `0` is a window at zero. They select the same rows and
    they are not the same request: one of them is a reader asking something.
    """
    for n in range(3):
        hub.send("tell", WRITER, READER, f"result {n}")
    hub.ack(READER, 3)
    monkeypatch.setenv("CAIRN_AGENT", READER)

    assert cli.run(["--hub", hub.base_url, "inbox", "--since", "0"]) == 1
    printed = capsys.readouterr().out

    assert "no unread messages" in printed
    assert "--since 0 is behind your read cursor at 3" in printed
    assert "cairn ack 0 --rewind" in printed


def test_a_window_at_zero_marks_nothing_read_like_every_other_window(hub, monkeypatch, capsys):
    """The one spelling of the flag that excludes nothing, and so could safely ack.

    It must not, and not for safety. The page it printed says nothing was marked
    read; acking here would make that footnote false on exactly one spelling,
    and a rule with one silent exception is a rule nobody can rely on.
    """
    for n in range(3):
        hub.send("tell", WRITER, READER, f"result {n}")
    monkeypatch.setenv("CAIRN_AGENT", READER)

    assert cli.run(["--hub", hub.base_url, "inbox", "--since", "0"]) == 0
    assert render.WINDOW_CLAUSE in capsys.readouterr().out
    assert len(hub.inbox(READER).messages) == 3, "--since 0 acked what it told the reader it had not"


def test_the_json_tells_no_window_apart_from_a_window_at_zero():
    """Same distinction, where a program can act on it."""
    assert json.loads(render.inbox_json([], total=0))["since"] is None
    assert json.loads(render.inbox_json([], total=0, since=0))["since"] == 0
