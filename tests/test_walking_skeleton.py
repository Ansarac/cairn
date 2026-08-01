"""The end-to-end test the whole design hangs on.

Everything else in the suite checks one module. This checks the seam: a real
hub on a real socket, a real client, two agents, a message that crosses between
them. It is the first test written and it should stay the first test read,
because the risks in this project live between modules rather than inside them.

If this file goes red, nothing else in the suite matters.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from cairn.client import HubClient
from cairn.errors import UsageError
from cairn.hub import make_server
from cairn.store import SqliteStore
from cairn.wire import Artifact


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


def _register(hub: HubClient, name: str, **kwargs) -> None:
    from cairn.wire import Agent

    hub.register(
        Agent(
            name=name,
            machine=kwargs.pop("machine", "testbox"),
            cwd=kwargs.pop("cwd", "/tmp/x"),
            **kwargs,
        )
    )


def test_a_name_that_moved_stops_reaching_its_old_holder(hub, tmp_path, monkeypatch):
    """Both halves of the takeover rule, over a real socket, in the order they fire.

    They are only meaningful together, which is why they are one test: the hub
    stops a newcomer inheriting the conversation, and the sender stops new mail
    following the name to whoever holds it now. Either alone leaves half the
    exchange going somewhere nobody chose.
    """
    from cairn.cli import _check_recipient
    from cairn.errors import NameMoved

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    _register(hub, "ops/dispatch", cwd="/w/ops")
    _register(hub, "rig/a", machine="bench", cwd="/w/rig")
    _check_recipient(hub, "rig/a")
    hub.send("tell", "ops/dispatch", "rig/a", "flash key is in the usual place")

    _register(hub, "rig/a", machine="some-other-box", cwd="/w/elsewhere")

    assert hub.inbox("rig/a") == []
    with pytest.raises(NameMoved) as caught:
        _check_recipient(hub, "rig/a")
    assert "bench:/w/rig" in str(caught.value)


def test_broadcast_has_no_holder_to_pin(hub, tmp_path, monkeypatch):
    """`*` is not an address anyone can take over, so the check must not fire on it."""
    from cairn.cli import _check_recipient

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    _register(hub, "ops/dispatch", cwd="/w/ops")
    _check_recipient(hub, "*")
    assert hub.send("tell", "ops/dispatch", "*", "bench down ten minutes").seq > 0


def test_a_message_crosses_between_two_agents(hub):
    """Register → tell → inbox → ack, over HTTP, with the cursor on the server."""
    _register(hub, "bench/firmware", capabilities=("hil", "jtag"))
    _register(hub, "compute/analysis", capabilities=("matlab",))

    sent = hub.send("tell", "bench/firmware", "compute/analysis", "soak run 441 failed 3 of 40 iterations")
    assert sent.seq > 0

    received = hub.inbox("compute/analysis")
    assert [m.body for m in received] == ["soak run 441 failed 3 of 40 iterations"]
    assert received[0].sender == "bench/firmware"

    hub.ack("compute/analysis", received[0].seq)
    assert hub.inbox("compute/analysis") == []


def test_the_sender_does_not_receive_its_own_message(hub):
    _register(hub, "a", machine="m1")
    _register(hub, "b", machine="m2")
    hub.send("tell", "a", "b", "hello")
    assert hub.inbox("a") == []


def test_a_peer_that_was_offline_still_gets_its_mail(hub):
    """The point of the hub: delivery does not require the peer to be listening."""
    _register(hub, "sender")
    _register(hub, "sleeper")
    for i in range(3):
        hub.send("tell", "sender", "sleeper", f"message {i}")

    # The sleeper "restarts": same name, no local state of any kind.
    _register(hub, "sleeper")
    assert [m.body for m in hub.inbox("sleeper")] == ["message 0", "message 1", "message 2"]


def test_a_brand_new_agent_starts_at_the_head_not_at_zero(hub):
    """A fresh name must not be buried under a month of other people's mail."""
    _register(hub, "a")
    _register(hub, "b")
    for i in range(5):
        hub.send("tell", "a", "b", f"old {i}")

    _register(hub, "newcomer")
    assert hub.inbox("newcomer") == []

    hub.send("tell", "a", "newcomer", "welcome")
    assert [m.body for m in hub.inbox("newcomer")] == ["welcome"]


def test_broadcast_reaches_everyone_but_the_sender(hub):
    for name in ("a", "b", "c"):
        _register(hub, name)
    hub.send("tell", "a", "*", "all hands")
    assert [m.body for m in hub.inbox("b")] == ["all hands"]
    assert [m.body for m in hub.inbox("c")] == ["all hands"]
    assert hub.inbox("a") == []


def test_a_typo_in_the_recipient_fails_loudly(hub):
    """Silently dropping a misaddressed message is the worst possible behaviour."""
    _register(hub, "a")
    _register(hub, "bench/firmware")
    with pytest.raises(UsageError, match="unknown recipient"):
        hub.send("tell", "a", "bench/firmwar", "typo")


def test_ask_and_reply_carry_the_correlation_id(hub):
    _register(hub, "asker")
    _register(hub, "answerer")
    hub.send("ask", "asker", "answerer", "do the failures correlate with temperature?", correlation_id="q-1")
    question = hub.inbox("answerer")[0]
    assert question.kind == "ask"
    assert question.correlation_id == "q-1"

    hub.send("reply", "answerer", "asker", "yes, all above 40 degrees", correlation_id="q-1")
    answer = hub.inbox("asker")[0]
    assert answer.kind == "reply"
    assert answer.correlation_id == "q-1"


def test_artifacts_survive_the_round_trip(hub):
    _register(hub, "a")
    _register(hub, "b")
    hub.send("tell", "a", "b", "capture is on the bench", artifacts=[Artifact("bench", "/srv/hil/441.bin")])
    artifact = hub.inbox("b")[0].artifacts[0]
    assert (artifact.host, artifact.path) == ("bench", "/srv/hil/441.bin")


def test_the_hub_is_healthy(hub):
    assert hub.health()["ok"] is True


# -- the bell stream -----------------------------------------------------------
#
# Same seam, one layer up: a real SSE response over a real socket, decoded by
# the same code the nudger uses. The bell is what makes delivery prompt; the
# inbox is what makes it correct. These assert the first without ever letting it
# carry the second.


def _bells(hub: HubClient, agent: str, want: int, timeout: float = 5.0) -> list[dict]:
    """Collect `want` bell payloads from a live stream, or fewer if it times out."""
    from cairn.events import sse_decode

    collected: list[dict] = []
    opened = threading.Event()

    def read() -> None:
        for event, payload in sse_decode(hub.stream(agent)):
            if event == "hello":
                opened.set()
            elif event == "mail":
                collected.append(payload)
                if len(collected) >= want:
                    return

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    assert opened.wait(timeout), "stream never opened"
    thread.join(timeout=timeout)
    return collected


def test_sending_a_message_rings_a_bell_on_the_stream(hub):
    _register(hub, "ringer")
    _register(hub, "listener")

    result: list[list[dict]] = []
    collector = threading.Thread(target=lambda: result.append(_bells(hub, "listener", want=1)), daemon=True)
    collector.start()
    time.sleep(0.4)  # let the subscription land before publishing

    hub.send("tell", "ringer", "listener", "the eval finished")
    collector.join(timeout=6.0)

    assert result, "collector thread never finished"
    bells = result[0]
    assert bells, "no bell arrived"
    assert bells[0]["seq"] > 0
    assert bells[0]["sender"] == "ringer"


def test_the_bell_never_carries_the_message_body(hub):
    """Invariant I1 at the transport layer: the stream signals, the inbox informs."""
    _register(hub, "ringer")
    _register(hub, "listener")
    body = "promote checkpoint 441 to staging immediately"

    result: list[list[dict]] = []
    collector = threading.Thread(target=lambda: result.append(_bells(hub, "listener", want=1)), daemon=True)
    collector.start()
    time.sleep(0.4)

    hub.send("tell", "ringer", "listener", body)
    collector.join(timeout=6.0)

    assert result, "collector thread never finished"
    bells = result[0]
    assert bells, "no bell arrived"
    assert body not in json.dumps(bells[0])
    assert "body" not in bells[0]


def test_the_events_route_demands_an_agent(hub):
    with pytest.raises(UsageError, match="agent"):
        next(iter(hub.stream("")))


def test_closing_the_notifier_ends_every_stream(hub, hub_server):
    """Hub shutdown must not leave handler threads blocked forever."""
    _register(hub, "listener")
    ended = threading.Event()

    def read() -> None:
        for _ in hub.stream("listener"):
            pass
        ended.set()

    threading.Thread(target=read, daemon=True).start()
    time.sleep(0.4)
    hub_server.notifier.close_all()
    assert ended.wait(timeout=5.0), "close_all left a stream open"
