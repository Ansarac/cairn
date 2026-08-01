"""The bell stream, checked where it is allowed to fail.

Most of these are ordinary round-trip tests. Two are not, and they are the ones
worth reading first:

`test_a_full_queue_drops_and_marks_instead_of_blocking_the_publisher` pins the
behaviour the module exists for. A subscriber that never reads must not be able
to slow down a publisher, because that publisher is a hub thread storing
somebody else's message. If someone ever "fixes" the dropping, this is what goes
red — and it goes red as a timeout, so the assertion says why.

`test_close_unblocks_a_reader_and_ends_the_iteration` pins the other half: a
stream that blocks forever is only safe if it can be woken.

Every wait here has a deadline. Nothing sleeps for longer than a frame or two,
and nothing touches the network or the disk.
"""

from __future__ import annotations

import threading
import time

import pytest

from cairn.events import SSE_RETRY_MS, Notifier, Subscription, heartbeat, sse_decode, sse_encode
from cairn.wire import BROADCAST

TIMEOUT = 2.0
"""Generous: every wait in this file should finish in milliseconds."""


def _take(sub, count, timeout=TIMEOUT):
    """Read `count` payloads, giving up by closing the stream so a bug fails instead of hanging."""
    guard = threading.Timer(timeout, sub.close)
    guard.start()
    try:
        taken = []
        for payload in sub:
            taken.append(payload)
            if len(taken) == count:
                break
        return taken
    finally:
        guard.cancel()


def _wait_until(predicate, timeout=TIMEOUT):
    """Poll `predicate` until it is true or the deadline passes; return whether it came true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# -- the codec ----------------------------------------------------------------


def test_a_frame_round_trips():
    payload = {"agent": "compute/analysis", "unread": 3, "note": "a bell — never content"}
    assert list(sse_decode([sse_encode("bell", payload)])) == [("bell", payload)]


def test_decode_reassembles_frames_delivered_one_byte_at_a_time():
    stream = sse_encode("bell", {"unread": 1}) + sse_encode("bell", {"unread": 2})
    chunks = [stream[i : i + 1] for i in range(len(stream))]
    assert [data["unread"] for _, data in sse_decode(chunks)] == [1, 2]


def test_decode_handles_a_split_inside_a_data_line():
    frame = sse_encode("bell", {"unread": 7, "agent": "compute/analysis"})
    cut = frame.index(b"data:") + 9
    assert list(sse_decode([frame[:cut], frame[cut:]])) == [("bell", {"unread": 7, "agent": "compute/analysis"})]


def test_decode_handles_crlf_split_between_the_cr_and_the_lf():
    frame = b'event: bell\r\ndata: {"unread":7}\r\n\r\n'
    cut = frame.index(b"\r\n") + 1
    assert list(sse_decode([frame[:cut], frame[cut:]])) == [("bell", {"unread": 7})]


def test_decode_ignores_heartbeats_and_unknown_fields():
    stream = heartbeat() + b"id: 17\nretry: 3000\n" + sse_encode("bell", {"unread": 1}) + heartbeat()
    assert list(sse_decode([stream])) == [("bell", {"unread": 1})]


def test_a_heartbeat_is_a_comment_and_yields_nothing():
    assert heartbeat().startswith(b":")
    assert heartbeat().endswith(b"\n\n")
    assert list(sse_decode([heartbeat() * 5])) == []


def test_a_frame_with_no_event_line_defaults_to_message():
    assert list(sse_decode([b'data: {"unread":1}\n\n'])) == [("message", {"unread": 1})]


def test_a_frame_the_stream_ended_mid_way_through_is_discarded():
    truncated = sse_encode("bell", {"unread": 1}).removesuffix(b"\n")
    assert list(sse_decode([truncated])) == []


def test_decode_skips_a_frame_whose_data_is_not_a_json_object():
    stream = b"event: bell\ndata: not json\n\n" + b"event: bell\ndata: [1,2]\n\n" + sse_encode("bell", {"unread": 1})
    assert list(sse_decode([stream])) == [("bell", {"unread": 1})]


def test_encode_refuses_an_event_name_that_would_corrupt_the_stream():
    with pytest.raises(ValueError, match="line break"):
        sse_encode("bell\ndata: injected", {"unread": 1})


def test_retry_hint_is_a_sane_reconnect_delay_in_milliseconds():
    assert 500 <= SSE_RETRY_MS <= 30_000


# -- fan-out ------------------------------------------------------------------


def test_subscription_knows_its_agent():
    notifier = Notifier()
    with notifier.subscribe("compute/analysis") as sub:
        assert isinstance(sub, Subscription)
        assert sub.agent == "compute/analysis"


def test_a_subscriber_gets_its_own_mail_and_not_a_peers():
    notifier = Notifier()
    with notifier.subscribe("compute/analysis") as mine, notifier.subscribe("bench/firmware") as theirs:
        notifier.publish("compute/analysis", {"unread": 1})
        notifier.publish("bench/firmware", {"unread": 2})
        assert _take(mine, 1) == [{"unread": 1}]
        assert _take(theirs, 1) == [{"unread": 2}]


def test_a_broadcast_reaches_every_subscriber():
    notifier = Notifier()
    with notifier.subscribe("compute/analysis") as a, notifier.subscribe("bench/firmware") as b:
        notifier.publish(BROADCAST, {"unread": 1})
        assert _take(a, 1) == [{"unread": 1}]
        assert _take(b, 1) == [{"unread": 1}]


def test_publishing_to_nobody_is_a_no_op():
    Notifier().publish("nobody/here", {"unread": 1})


def test_a_full_queue_drops_and_marks_instead_of_blocking_the_publisher():
    """The property the module exists for. A dead reader must cost latency, never delivery."""
    notifier = Notifier(queue_size=2)
    with notifier.subscribe("compute/analysis") as sub:  # nothing ever reads this one
        started = time.monotonic()
        for i in range(200):
            notifier.publish("compute/analysis", {"unread": i})
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"publish took {elapsed:.3f}s — it waited on a subscriber that never reads"
        assert sub.dropped() == 198
        assert _take(sub, 2) == [{"unread": 0}, {"unread": 1}]


def test_close_unblocks_a_reader_and_ends_the_iteration():
    notifier = Notifier()
    sub = notifier.subscribe("compute/analysis")
    seen = []
    finished = threading.Event()

    def reader():
        seen.extend(sub)  # blocks in __iter__ until the stream ends
        finished.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.05)  # let it actually reach the block
    assert not finished.is_set()

    sub.close()
    sub.close()  # idempotent

    assert finished.wait(TIMEOUT), "close() left a reader blocked in __iter__"
    thread.join(TIMEOUT)
    assert not thread.is_alive()
    assert seen == []


def test_a_closed_subscriber_stops_receiving():
    notifier = Notifier()
    sub = notifier.subscribe("compute/analysis")
    sub.close()
    notifier.publish("compute/analysis", {"unread": 1})
    assert list(sub) == []


def test_subscriber_count_tracks_subscribe_close_and_unsubscribe():
    notifier = Notifier()
    assert notifier.subscriber_count() == 0

    first = notifier.subscribe("compute/analysis")
    second = notifier.subscribe("compute/analysis")
    third = notifier.subscribe("bench/firmware")
    assert notifier.subscriber_count("compute/analysis") == 2
    assert notifier.subscriber_count() == 3

    notifier.unsubscribe(first)
    assert notifier.subscriber_count("compute/analysis") == 1

    second.close()  # a dropped connection is forgotten without an explicit unsubscribe
    assert notifier.subscriber_count("compute/analysis") == 0

    notifier.unsubscribe(third)
    notifier.unsubscribe(third)  # idempotent
    assert notifier.subscriber_count() == 0


def test_leaving_the_context_manager_unsubscribes():
    notifier = Notifier()
    with notifier.subscribe("compute/analysis") as sub:
        assert notifier.subscriber_count("compute/analysis") == 1
    assert sub.closed
    assert notifier.subscriber_count("compute/analysis") == 0


def test_concurrent_publishers_lose_nothing_for_a_reader_that_keeps_up():
    publishers, per_publisher = 4, 250
    total = publishers * per_publisher
    notifier = Notifier(queue_size=total)  # sized so a slow reader cannot be the reason for a drop
    received = []

    with notifier.subscribe("compute/analysis") as sub:
        reader = threading.Thread(target=lambda: received.extend(sub), daemon=True)
        reader.start()

        threads = [
            threading.Thread(target=_publish_run, args=(notifier, publisher, per_publisher))
            for publisher in range(publishers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(TIMEOUT)
            assert not thread.is_alive(), "a publisher never returned"

        assert _wait_until(lambda: len(received) == total), f"reader saw {len(received)} of {total}"

    reader.join(TIMEOUT)
    assert not reader.is_alive()
    assert sub.dropped() == 0
    assert {(bell["publisher"], bell["n"]) for bell in received} == {
        (publisher, n) for publisher in range(publishers) for n in range(per_publisher)
    }


def _publish_run(notifier, publisher, count):
    """Ring `count` bells from one thread, tagged so the reader can prove none were mangled."""
    for n in range(count):
        notifier.publish("compute/analysis", {"publisher": publisher, "n": n})


def test_close_all_unblocks_every_reader():
    """Hub shutdown: without this, each SSE handler thread blocks forever and the process hangs."""
    notifier = Notifier()
    subs = [notifier.subscribe(f"agent-{i}") for i in range(3)]
    done = threading.Event()
    seen: list[str] = []

    def drain() -> None:
        for sub in subs:
            for _ in sub:
                pass
            seen.append(sub.agent)
        done.set()

    threading.Thread(target=drain, daemon=True).start()
    notifier.close_all()
    assert done.wait(timeout=2.0), "close_all left a reader blocked"
    assert len(seen) == 3
    assert notifier.subscriber_count() == 0


def test_close_all_is_idempotent_and_publish_after_it_is_harmless():
    notifier = Notifier()
    notifier.subscribe("a")
    notifier.close_all()
    notifier.close_all()
    notifier.publish("a", {"unread": 1})
    assert notifier.subscriber_count() == 0
