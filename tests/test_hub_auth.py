"""The door on the hub, and the two things it must not become.

The feature is small — one header, one comparison. What is worth testing is
mostly its edges: that an unconfigured hub is unchanged, that the health route
still answers a healthcheck, that a refusal is not reported as an outage, and
above all that authenticating a *connection* never turns into a claim about who
sent a *message*. That last one is invariant I1 and is asserted end to end in
`test_walking_skeleton.py`, where a real hub and a real reading make it mean
something.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from cairn import config
from cairn.client import HubClient
from cairn.errors import Unauthorized, Unreachable
from cairn.hub import make_server
from cairn.store import SqliteStore
from cairn.wire import Agent

TOKEN = "a-shared-secret-for-this-test"  # noqa: S105 - a literal in a test, not a credential


@pytest.fixture
def secured() -> Iterator[str]:
    """Serve a hub that requires `TOKEN`, and yield its base URL."""
    server = make_server(SqliteStore(":memory:"), host="127.0.0.1", port=0, token=TOKEN)
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


@pytest.fixture
def open_hub() -> Iterator[str]:
    """Serve a hub with no token at all — every build before this cut."""
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


def _agent(name: str = "bench") -> Agent:
    return Agent(name=name, machine="testbox", cwd="/tmp/x")


# -- the resolver ------------------------------------------------------------


def test_no_token_configured_is_the_ordinary_answer():
    assert config.token() is None


def test_the_environment_beats_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "cairn" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('hub = "http://x"\ntoken = "from-the-file"\n', encoding="utf-8")
    assert config.token() == "from-the-file"

    monkeypatch.setenv("CAIRN_TOKEN", "from-the-environment")
    assert config.token() == "from-the-environment"


def test_an_empty_token_is_no_token(monkeypatch, tmp_path):
    """An empty string must not secure a hub that nobody can then talk to."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "cairn" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('token = ""\n', encoding="utf-8")
    assert config.token() is None

    monkeypatch.setenv("CAIRN_TOKEN", "")
    assert config.token() is None


def test_a_world_readable_config_holding_a_token_says_so(monkeypatch, tmp_path, capsys):
    """Cairn cannot chmod the operator's file, so it has to say something."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "cairn" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('token = "secret"\n', encoding="utf-8")
    path.chmod(0o644)

    assert config.token() == "secret"
    warning = capsys.readouterr().err
    assert "readable by others" in warning
    assert "chmod 600" in warning
    assert "secret" not in warning, "the warning must not print the thing it is warning about"


# -- the diagnostic ----------------------------------------------------------


def _config_page(capsys) -> str:
    from cairn import cli

    assert cli.run(["config"]) == 0
    return capsys.readouterr().out


def test_config_says_when_no_token_is_set(capsys):
    assert "token        not set" in _config_page(capsys)


def test_config_names_the_config_file_as_the_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "cairn" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('token = "from-the-file"\n', encoding="utf-8")
    path.chmod(0o600)
    assert "token        set (config file)" in _config_page(capsys)


def test_config_names_the_environment_when_it_is_the_one_winning(monkeypatch, tmp_path, capsys):
    """The failure this whole line exists for: an override nobody remembered setting.

    A file with a token in it *and* an environment variable is not a contrived
    case — it is what the machine looks like halfway through configuring one.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "cairn" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('token = "from-the-file"\n', encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("CAIRN_TOKEN", "from-the-environment")
    assert "token        set ($CAIRN_TOKEN)" in _config_page(capsys)


def test_config_never_prints_the_token(monkeypatch, tmp_path, capsys):
    """`cairn config` is what somebody pastes when asking for help.

    A page that leaks the secret is a page that cannot be shared, which would
    cost the diagnostic exactly the situation it was added for.
    """
    monkeypatch.setenv("CAIRN_TOKEN", "hunter2-do-not-print-me")
    page = _config_page(capsys)
    assert "hunter2-do-not-print-me" not in page
    assert "set ($CAIRN_TOKEN)" in page


# -- the door ----------------------------------------------------------------


def test_an_unconfigured_hub_is_exactly_what_it_was(open_hub):
    """The default has to stay the old behaviour, or every peer breaks on upgrade."""
    client = HubClient(open_hub, timeout=5.0)
    client.register(_agent())
    assert [a.name for a in client.peers()] == ["bench"]


def test_a_secured_hub_refuses_a_client_with_no_token(secured):
    client = HubClient(secured, timeout=5.0)
    with pytest.raises(Unauthorized):
        client.register(_agent())


def test_a_secured_hub_refuses_the_wrong_token(secured, monkeypatch):
    monkeypatch.setenv("CAIRN_TOKEN", "not-the-one")
    client = HubClient(secured, timeout=5.0)
    with pytest.raises(Unauthorized):
        client.peers()


def test_the_right_token_gets_in(secured, monkeypatch):
    monkeypatch.setenv("CAIRN_TOKEN", TOKEN)
    client = HubClient(secured, timeout=5.0)
    client.register(_agent())
    assert [a.name for a in client.peers()] == ["bench"]


def test_a_refusal_is_not_an_outage(secured):
    """Exit 4, not exit 2.

    The whole reason `Unauthorized` exists rather than reusing `Unreachable`: a
    script retries an outage forever and gets nowhere, because the hub is fine
    and the credential is wrong. `errors.py`'s docstring carries the argument.
    """
    client = HubClient(secured, timeout=5.0)
    with pytest.raises(Unauthorized) as caught:
        client.peers()
    assert caught.value.exit_code == 4
    assert not isinstance(caught.value, Unreachable)
    said = str(caught.value)
    assert "CAIRN_TOKEN" in said, "the message has to say where to put one"
    assert "Retrying will not help" in said


def test_the_refusal_names_the_hub_and_not_the_hub_s_own_words(secured):
    """The message is written locally, and that is deliberate.

    The hub cannot know where this machine keeps its config, so it is the one
    party unable to give useful advice here. Not quoting its reply also keeps a
    wire-supplied string out of a print path — invariant I1, column zero.
    """
    client = HubClient(secured, timeout=5.0)
    with pytest.raises(Unauthorized) as caught:
        client.peers()
    assert secured in str(caught.value)
    assert "this hub requires a token" not in str(caught.value), "that is the hub's wording, not ours"


def test_the_bell_stream_carries_the_token_too(secured, monkeypatch):
    """The route that a unit test would have left open.

    `stream()` builds its own request, so an inline header on `_call` secures
    everything except the bell. Both previous defects in this path were invisible
    to unit tests, which is why this one is asserted from both sides: refused
    without the token, and opening with it.
    """
    from cairn.events import sse_decode

    client = HubClient(secured, timeout=5.0)
    with pytest.raises(Unauthorized):
        next(iter(client.stream("listener")))

    monkeypatch.setenv("CAIRN_TOKEN", TOKEN)
    authorized = HubClient(secured, timeout=5.0)
    # `next`, not a zip or a comprehension with a bound. `zip(events, range(1))`
    # pulls from the stream *before* it discovers the counter is exhausted, so it
    # blocks for a whole heartbeat on a quiet hub — a passing assertion twenty
    # seconds later, or a hang if the fixture tears down first.
    event, _payload = next(iter(sse_decode(authorized.stream("listener"))))
    assert event == "hello", "the stream never opened for a client holding the token"


# -- the health route --------------------------------------------------------


def test_health_answers_a_healthcheck_that_holds_no_token(secured):
    """`compose.yaml` calls this from inside the container with no credential.

    A hub that reports unhealthy the moment it is secured would be discovered at
    the worst possible moment, by whoever just turned authentication on.
    """
    client = HubClient(secured, timeout=5.0)
    assert client.health()["ok"] is True


def test_health_tells_a_stranger_nothing_but_liveness(secured, monkeypatch):
    """The agent count is the one thing this route knows that a stranger should not."""
    monkeypatch.setenv("CAIRN_TOKEN", TOKEN)
    HubClient(secured, timeout=5.0).register(_agent())

    monkeypatch.delenv("CAIRN_TOKEN")
    assert "agents" not in HubClient(secured, timeout=5.0).health()

    monkeypatch.setenv("CAIRN_TOKEN", TOKEN)
    assert HubClient(secured, timeout=5.0).health()["agents"] == 1


def test_an_open_hub_still_counts_for_anyone(open_hub):
    client = HubClient(open_hub, timeout=5.0)
    client.register(_agent())
    assert client.health()["agents"] == 1


def test_a_refused_post_does_not_poison_the_next_request_on_that_connection(secured):
    """The refused request is not the casualty; the one after it is.

    A 401 answered without reading the POST body leaves that body in the socket
    where HTTP/1.1's next request line belongs. Measured before the fix: this
    second call came back **400**, for a reason having nothing to do with what it
    asked. cairn's own client cannot reach it — `urllib` opens a fresh connection
    every time — so nothing in the suite would have caught it without going to
    `http.client`, which keeps the connection open the way a proxy would.
    """
    import http.client
    import json as _json

    host, port = secured.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        body = _json.dumps({"name": "x", "machine": "m", "cwd": "/c"})
        conn.request("POST", "/v1/register", body=body, headers={"Content-Type": "application/json"})
        refused = conn.getresponse()
        refused.read()
        assert refused.status == 401

        conn.request("GET", "/v1/health")
        after = conn.getresponse()
        after.read()
        assert after.status == 200, "the refused body was left in the socket and ate the next request"
    finally:
        conn.close()


def test_a_stranger_cannot_map_the_routes(secured):
    """401 before the route table, so a missing route and a real one look alike.

    Otherwise the refusal is a directory: 404 for a route that does not exist
    against 401 for one that does tells an unauthenticated caller the shape of
    the API for free.
    """
    import urllib.error
    import urllib.request

    codes = []
    for path in ("/v1/peers", "/v1/no-such-route"):
        try:
            urllib.request.urlopen(f"{secured}{path}", timeout=5)  # noqa: S310 - loopback, built here
        except urllib.error.HTTPError as exc:
            codes.append(exc.code)
    assert codes == [401, 401]
