"""The page, the backlog behind it, and the difference nothing used to report.

`tests/test_walking_skeleton.py` holds the reason this exists — a turn-boundary
bell that went permanently silent once the unread count passed `--limit`. These
are the pieces of that arithmetic, checked one at a time so that a regression
says which one moved.

The through-line: `messages` is capped and `unread`/`head` are not, and every
caller that inferred either of the latter from the former was wrong in a way
nothing announced.
"""

from __future__ import annotations

import json

import pytest

from cairn import render
from cairn.store import SqliteStore
from cairn.wire import Agent, InboxEntry, InboxPage, Message, Provenance


def _store() -> SqliteStore:
    store = SqliteStore(":memory:")
    for name in ("bench/firmware", "compute/analysis"):
        store.register(Agent(name=name, machine=name.split("/")[0], cwd=f"/w/{name}"))
    return store


def _entry(seq: int) -> InboxEntry:
    message = Message(seq=seq, kind="tell", sender="compute/analysis", recipient="bench/firmware", body=f"body {seq}")
    return InboxEntry(message=message, provenance=Provenance.unverified("nothing was checked"))


# -- the store -----------------------------------------------------------------


def test_the_count_and_the_head_ignore_the_limit():
    """Both are `COUNT` and `MAX` over the whole backlog, not over what fitted."""
    store = _store()
    for n in range(7):
        store.append("tell", "compute/analysis", "bench/firmware", f"result {n}")

    page = store.unread("bench/firmware", limit=2)

    assert len(page.messages) == 2, "the page ignored the limit"
    assert page.unread == 7
    assert page.head == 7, "the head stopped at the end of the page, which is the deafness"
    assert page.truncated


def test_the_page_is_the_oldest_end_of_the_queue():
    """A queue is read from the front, unlike notes and the sent log.

    Load-bearing rather than a preference: a `--wait` may only ever run on an
    empty window precisely because a poll loop over a truncated one would never
    reach the newest message, and that is only true while the page is the oldest
    rows. Flipping this to match the other two paged surfaces would make the
    waiter unsound without changing a line of `waiting.py`.
    """
    store = _store()
    sent = [store.append("tell", "compute/analysis", "bench/firmware", f"result {n}") for n in range(5)]

    page = store.unread("bench/firmware", limit=2)

    assert [m.seq for m in page.messages] == [sent[0].seq, sent[1].seq]


def test_the_head_counts_only_what_this_agent_may_read():
    """Same predicate as the page: past the cursor, addressed here, not its own sends.

    A head computed over `messages` at large would ring a bell for somebody
    else's traffic — and, worse, latch on it, so the reader would be told about
    mail it can never see and then told nothing when its own arrived.
    """
    store = _store()
    mine = store.append("tell", "compute/analysis", "bench/firmware", "for me")
    store.append("tell", "bench/firmware", "compute/analysis", "my own send, a higher seq")

    page = store.unread("bench/firmware", limit=50)

    assert page.unread == 1
    assert page.head == mine.seq


def test_an_acknowledged_backlog_leaves_no_head_behind():
    """The head follows the cursor, or a drained mailbox would ring forever."""
    store = _store()
    for n in range(4):
        store.append("tell", "compute/analysis", "bench/firmware", f"result {n}")
    store.ack("bench/firmware", 4)

    page = store.unread("bench/firmware", limit=50)

    assert (page.unread, page.head, page.truncated) == (0, 0, False)


def test_counting_a_takeover_s_skipped_backlog_needs_no_page():
    """`register` reports what a takeover stepped over, and now counts it without listing it.

    The count used to be `len(unread(limit=1_000_000))` — a constant whose only
    job was to be larger than any real backlog, materialising every skipped row
    to arrive at an integer. It is the same number either way; what changed is
    that a wrong limit can no longer understate a loss the report exists to
    state.
    """
    store = _store()
    for n in range(120):
        store.append("tell", "compute/analysis", "bench/firmware", f"result {n}")

    moved = store.register(Agent(name="bench/firmware", machine="elsewhere", cwd="/w/other"))

    assert moved.arrival == "takeover"
    assert moved.skipped == 120


# -- the wire shape ------------------------------------------------------------


def test_a_hub_that_sends_no_totals_leaves_the_page_speaking_for_itself():
    """The cross-version case: absent is "I cannot tell you", not zero.

    A hub built before this cut answers `/v1/inbox` with `messages` alone. That
    is not an outage and must not be reported as one, so the totals fall back to
    what the page can say — which reproduces exactly what this client did before
    they existed, deafness included. Degrading to the old behaviour is honest;
    refusing to speak to the hub over two keys neither end needs to agree on
    would not be.
    """
    old = {"messages": [Message(seq=4, kind="tell", sender="a", recipient="b", body="hi").to_json()]}

    page = InboxPage.from_json(old)

    assert page.unread == 1
    assert page.head == 4
    assert not page.truncated


def test_an_empty_page_from_a_new_hub_is_not_mistaken_for_an_old_one():
    """`unread: 0` is an answer. Reading it as "absent" would send the caller down the fallback."""
    page = InboxPage.from_json({"messages": [], "unread": 0, "head": 0})

    assert (page.unread, page.head) == (0, 0)


def test_the_totals_survive_the_round_trip():
    """What the hub serializes is what the client parses, including a truncated page."""
    original = InboxPage(messages=(Message(seq=2, kind="tell", sender="a", recipient="b", body="x"),), unread=9, head=9)

    restored = InboxPage.from_json(json.loads(json.dumps(original.to_json())))

    assert restored == original
    assert restored.truncated


# -- the rendering -------------------------------------------------------------


def test_the_header_counts_the_backlog_and_the_line_counts_the_page():
    """Two numbers saying two things, and the word "unread" means the one it always meant."""
    text = render.inbox_text([_entry(1), _entry(2)], total=9)

    assert text.splitlines()[0].startswith("cairn inbox: 9 unread")
    assert "showing the oldest 2 of 9" in text


def test_a_complete_page_says_nothing_about_truncation():
    """A line that appears when it need not is one a reader learns to skip."""
    assert "showing the oldest" not in render.inbox_text([_entry(1)], total=1)


def test_the_truncation_line_arrives_before_the_first_message():
    """Its position is the whole of its value.

    In the footnotes it lands after the reader has formed a view of the mailbox,
    which is after the damage. Same placement and same reasoning as `notes` and
    the sent log — one helper, so the three cannot drift.
    """
    lines = render.inbox_text([_entry(1), _entry(2)], total=9).splitlines()

    assert lines.index("— showing the oldest 2 of 9; raise --limit for the rest") < next(
        i for i, line in enumerate(lines) if line.startswith("[1]")
    )


def test_json_reports_the_backlog_and_the_page_separately():
    """`unread` keeps its name and gains its meaning; `showing` is the page, as in `notes --json`."""
    payload = json.loads(render.inbox_json([_entry(1)], total=6))

    assert payload["unread"] == 6
    assert payload["showing"] == 1
    assert len(payload["messages"]) == 1


@pytest.mark.parametrize("total", [0, 3])
def test_an_empty_page_never_reads_as_an_empty_mailbox(total):
    """Nothing shown over something waiting is the one answer a reader acts on unchecked.

    Unreachable from the CLI, which now refuses a page of zero — kept honest at
    the renderer anyway, because the rule that an answer of "nothing" must be
    true has been re-learned on three surfaces already.
    """
    text = render.inbox_text([], total=total)

    assert ("no unread messages" in text) is (total == 0)
    if total:
        assert f"{total} unread, none of them shown" in text
