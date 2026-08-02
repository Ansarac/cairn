"""Whose clock the ages are measured on, and what happens when the two disagree.

`docs/design.md` §12 item 7's tail is the evidence: a session asked whether the
overnight window was still open — the most decision-relevant fact in a shift
handover — and could not answer it, because `peers` prints ages computed against
an instant nothing in the output names.

Underneath the missing anchor was a defect nobody had looked for. `last_seen` is
stamped by the **hub**; the age was computed against the **reader's** clock. On
one machine those agree by construction, and one machine is not what cairn is
for. Two clocks were being subtracted from each other and the difference landed
silently in every age on the page.

So the fix is two things that look like one: the hub's clock rides the envelope
of every response, and `_ago` does its arithmetic on that clock rather than the
local one. The anchor a reader was missing falls out of the same field.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from cairn import cli, render
from cairn.client import HubClient
from cairn.hub import make_server
from cairn.store import SqliteStore
from cairn.wire import Agent, envelope, now


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


def _stamp(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _agent(seen: str) -> Agent:
    return Agent(name="bench/firmware", machine="bench", cwd="/w/fw", last_seen=seen)


# -- the arithmetic ------------------------------------------------------------


def test_an_age_is_measured_on_the_clock_that_stamped_it():
    """The defect: a hub-stamped instant subtracted from the reader's own clock.

    Staged as an hour of skew, which is what a machine with the wrong timezone
    offset or a stopped NTP produces. The row was written one minute before the
    hub's clock; a reader an hour behind used to be told it was an hour old, and
    a reader an hour ahead that a dead session had just spoken.
    """
    written = datetime.now(UTC) - timedelta(minutes=1)
    hub_clock = written + timedelta(seconds=30)
    stamp = written.isoformat().replace("+00:00", "Z")

    assert render._ago(stamp, hub_clock.isoformat().replace("+00:00", "Z")) == "just now"
    assert render._ago(stamp, (hub_clock + timedelta(hours=1)).isoformat().replace("+00:00", "Z")) == "1h ago"


def test_no_hub_clock_falls_back_to_this_machine_and_nothing_else_changes():
    """What an older hub gets, and it is the behaviour that shipped for eight cuts.

    There is nothing better available against a hub that sends no time of its
    own, and refusing to render an age over it would be worse than the skew it
    was already living with.
    """
    assert render._ago(_stamp(hours=6)) == "6h ago"
    assert render._ago(_stamp(hours=6), "") == "6h ago"


def test_a_hub_clock_this_cannot_read_costs_the_anchor_and_never_the_age():
    """One bad field in the envelope must not turn every age back into a raw stamp.

    Including the naive one, which is the interesting half: `2026-08-01T00:00:00`
    *parses* and then raises on the subtraction — a `TypeError` that `cli.run`
    does not catch, so a traceback and exit 1 out of `cairn peers`.
    """
    for unreadable in ("not a timestamp", "2026-08-01T00:00:00", ""):
        assert render._ago(_stamp(hours=6), unreadable) == "6h ago"


def test_an_unreadable_stamp_is_still_handed_back_rather_than_guessed_at():
    """The older rule, unchanged: rendering `peers` is not the place to raise."""
    assert render._ago("not a timestamp", now()) == "not a timestamp"
    assert render._ago("", now()) == ""


# -- the envelope --------------------------------------------------------------


def test_every_response_carries_the_hub_s_clock():
    """On the envelope rather than a route, because the alternative is a round trip."""
    wrapped = envelope({"agents": []})

    assert wrapped["t"], "no response says what time the hub thinks it is"
    assert render._instant(wrapped["t"]) is not None


def test_the_client_records_the_hub_clock_off_a_call_it_was_making_anyway(hub):
    """Read on the way past, so no command spends a second round trip on it."""
    assert hub.hub_time == "", "a client that has made no call cannot know the hub's time"

    hub.peers()

    assert render._instant(hub.hub_time) is not None


def test_an_old_hub_leaves_the_last_known_time_alone_rather_than_blanking_it(hub, monkeypatch):
    """Absence is "this hub cannot tell me", which must not erase what another call did."""
    hub.peers()
    remembered = hub.hub_time

    monkeypatch.setattr(HubClient, "_call", lambda self, *a, **k: {"agents": []})  # noqa: ARG005
    hub.peers()

    assert hub.hub_time == remembered


def test_the_hub_does_not_read_the_clock_a_client_sends_it(hub):
    """A timestamp from a peer is an assertion about that peer, like every other field.

    The envelope is one function, so a client's own POSTs carry `t` as well. The
    hub stamps its rows with its own `now()` and must keep doing so — the same
    rule that keeps `verified` off `Message`.
    """
    hub.register(Agent(name="bench/firmware", machine="bench", cwd="/w/fw"))
    hub._call(
        "POST",
        "/v1/messages",
        {"kind": "tell", "sender": "bench/firmware", "recipient": "*", "body": "hi", "t": "1999-01-01T00:00:00Z"},
    )

    stored = hub.sent("bench/firmware")[0][0]

    assert stored.created_at > "2020", f"the hub took a timestamp off the wire: {stored.created_at}"


# -- the reading ---------------------------------------------------------------


def test_the_anchor_is_a_footnote_and_says_which_clock_it_is():
    """Not the header and not a stamp per row; see `_clock_notes` for both refusals."""
    stamped = render.peers_text([_agent(_stamp(hours=6))], now=now())
    local = render.peers_text([_agent(_stamp(hours=6))])

    assert stamped.splitlines()[0] == "cairn: 1 other agent registered"
    assert "— hub clock 2026-" in stamped
    assert "— this machine's clock 2026-" in local, "an older hub's fallback must not claim to be the hub's clock"


def test_the_row_still_shows_the_age_and_not_the_stamp():
    """The decision the anchor was tempted to undo, kept.

    Absolute-only is what made two live sessions do the subtraction in their
    heads, and one of them nearly handed a job to a session that had ended. The
    anchor answers "what time is it", which is one fact per reading; it does not
    licence putting the arithmetic back on every row.
    """
    text = render.peers_text([_agent(_stamp(hours=6))], now=now())

    assert "(seen 6h ago)" in text


def test_a_material_disagreement_between_the_two_clocks_is_said_out_loud():
    """Because every age on the page is off by it, and looks self-consistent anyway."""
    # Half a minute clear of the boundary in both directions: the stamps are
    # second-resolution, so an exact 300 lands on 299 and `_span` floors it to 4m.
    behind = render.peers_text([_agent(_stamp(hours=1))], now=_stamp(seconds=330))
    ahead = render.peers_text([_agent(_stamp(hours=1))], now=_stamp(seconds=-330))

    assert "this machine's clock is 5m ahead of the hub's" in behind
    assert "this machine's clock is 5m behind the hub's" in ahead


def test_two_clocks_that_agree_say_nothing_about_it():
    """A line that is always there is a line nobody reads."""
    text = render.peers_text([_agent(_stamp(hours=1))], now=now())

    assert "— hub clock" in text
    assert "ahead of the hub" not in text
    assert "behind the hub" not in text


def test_the_json_carries_the_clock_its_ages_were_measured_against():
    """A program recomputing an age from `last_seen` must not use its own."""
    payload = json.loads(render.peers_json([_agent(_stamp(hours=1))], "2026-08-02T09:00:00Z"))

    assert payload["now"] == "2026-08-02T09:00:00Z"
    assert render._instant(json.loads(render.peers_json([]))["now"]) is not None, "the key must never be absent"


def test_a_pile_of_notes_says_what_its_dates_are_old_relative_to():
    """`STALENESS_CLAUSE` asks for a judgement and used to withhold half of it.

    Notes are the surface where the gap is months rather than minutes: a reader
    told that a note "is what one peer believed at the time shown" cannot act on
    that without knowing what time it is now, and nothing in the reading said.
    """
    from cairn.wire import Note, NoteEntry

    entry = NoteEntry(note=Note(id=1, subject="rig-a", author="bench/firmware", body="chamber overshoots ~2C"))
    lines = render.notes_text([entry], 1, "rig-a", now=now()).rstrip().splitlines()

    anchor = next(i for i, line in enumerate(lines) if line.startswith("— hub clock"))
    clause = next(i for i, line in enumerate(lines) if render.STALENESS_CLAUSE in line)
    assert clause < anchor, "the clause asks the question; the anchor is the half it was missing"
    assert lines[anchor] == lines[-1], "two clocks agree here, so nothing should follow the anchor"


def test_the_subject_index_carries_the_clock_its_last_dates_are_read_against():
    """It is read to decide what is worth opening, and `last` is the deciding column."""
    from cairn.wire import SubjectSummary

    index = [SubjectSummary(subject="rig-a", notes=3, open_questions=1, last_at="2026-06-01T00:00:00Z")]

    assert "— hub clock 2026-08-02T09:00:00Z" in render.subjects_text(index, now="2026-08-02T09:00:00Z")


def test_the_inbox_and_the_sent_log_deliberately_carry_none_of_this():
    """The line this rule stops at, pinned, because an unstated line is one that drifts.

    Both print times and neither asks the reader to weigh elapsed time: the inbox
    asks it to act on content, and everything in it is by construction newer than
    a cursor the reader has just moved. `_asked`'s lesson is that a rule applied to
    three surfaces out of four is one a reader stops trusting, so where this one
    stops is a decision rather than an omission — and docs/design.md §12 item 12
    names it as the weakest part of the change.
    """
    from cairn.wire import InboxEntry, Message, SentEntry

    message = Message(seq=1, kind="tell", sender="a", recipient="b", body="x")

    assert "hub clock" not in render.inbox_text([InboxEntry(message=message, provenance=_unverified())], 1)
    assert "hub clock" not in render.sent_text([SentEntry(message=message)], 1)


def _unverified():
    from cairn.wire import Provenance

    return Provenance.unverified("nothing was checked")


def test_peers_over_a_real_socket_reports_the_hub_s_clock(hub, monkeypatch, capsys):
    """End to end, because the anchor is only true if it survived the wire.

    In process the hub's clock and the reader's are the same clock, so this
    cannot show the skew case — what it does show is that the field is populated
    from the response rather than from `datetime.now` on this side.
    """
    hub.register(Agent(name="bench/firmware", machine="bench", cwd="/w/fw"))
    hub.register(Agent(name="compute/analysis", machine="compute", cwd="/w/an"))
    monkeypatch.setenv("CAIRN_AGENT", "bench/firmware")

    assert cli.run(["--hub", hub.base_url, "peers"]) == 0
    printed = capsys.readouterr().out

    assert "— hub clock 2026-" in printed
    assert "this machine's clock" not in printed
    assert "(seen just now)" in printed
