"""The two places where a bug is invisible from the other side.

`locate_skill` has a source branch and a packaged branch, and a user only ever
exercises one of them. `merge_hooks` edits a file that belongs to another
program, so it has to be idempotent and it has to leave other hooks alone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cairn.adapters import claude_code
from cairn.errors import UsageError
from cairn.skill import install_skill, locate_skill


def test_skill_is_found_in_a_source_checkout():
    path = locate_skill()
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("---\nname: cairn")


def test_skill_is_found_when_packaged(tmp_path, monkeypatch):
    """`uv tool install` ships SKILL.md next to the module; that branch must work too."""
    import cairn.skill as skill_module

    fake_pkg = tmp_path / "cairn"
    (fake_pkg / "_skill").mkdir(parents=True)
    (fake_pkg / "_skill" / "SKILL.md").write_text("---\nname: cairn\n---\npackaged\n", encoding="utf-8")
    monkeypatch.setattr(skill_module, "__file__", str(fake_pkg / "skill.py"))
    assert locate_skill().read_text(encoding="utf-8").endswith("packaged\n")


def test_a_broken_build_says_so(tmp_path, monkeypatch):
    import cairn.skill as skill_module

    monkeypatch.setattr(skill_module, "__file__", str(tmp_path / "nowhere" / "skill.py"))
    with pytest.raises(UsageError, match="this build is broken"):
        locate_skill()


def test_install_skill_writes_where_it_is_told(tmp_path):
    target = install_skill(tmp_path / "skills" / "cairn")
    assert target.is_file()
    assert "cairn inbox" in target.read_text(encoding="utf-8")


def _hook_input(event: str) -> str:
    return json.dumps({"hook_event_name": event, "session_id": "abc", "cwd": "/w/bench"})


def test_the_turn_boundary_bell_asks_for_another_turn():
    payload = claude_code.bell_payload(_hook_input(claude_code.TURN_BOUNDARY), "9 unread")
    assert payload == {"decision": "block", "reason": "9 unread"}


def test_the_session_start_bell_speaks_the_other_envelope():
    """The same sentence, and it reaches nobody in the other shape.

    `decision` is a turn-boundary mechanism. On `SessionStart` the host takes the
    payload, records it as a hook error with the text on stderr, and shows the
    model nothing — measured with marker hooks on 2.1.220, and measured again
    with a live session that was asked, tools forbidden, what was in its context
    at startup: *"No such thing is in my context."*
    """
    payload = claude_code.bell_payload(_hook_input(claude_code.SESSION_START), "9 unread")
    assert payload == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "9 unread"}}


@pytest.mark.parametrize("said", ["", "not json", "[]", "null", '{"hook_event_name": 7}', "{}"])
def test_a_host_that_names_no_event_gets_the_turn_boundary_shape(said):
    """Silence is the one answer this must not give.

    Run by hand there is no host and no event. Falling back to no bell at all
    would make a hand-run `cairn bell` look broken, and would turn any future
    change in how the event is announced into the exact failure this envelope
    split exists to fix.
    """
    assert claude_code.bell_payload(said, "9 unread") == {"decision": "block", "reason": "9 unread"}


def test_every_installed_event_has_an_envelope_that_was_measured():
    """A third hook must not be installed on an unmeasured shape.

    This is the whole lesson of the defect, as a test: the payload the host
    accepts is per-event, a wrong one fails silently, and `cli.cmd_bell` latches
    on the ring rather than on the reading — so an event installed without a
    measured envelope does not merely lose its own bell, it eats the next one
    from an event that works.
    """
    assert set(claude_code.hook_config()) == {claude_code.TURN_BOUNDARY, claude_code.SESSION_START}


def test_merging_hooks_twice_produces_one_bell():
    once = claude_code.merge_hooks({})
    twice = claude_code.merge_hooks(once)
    assert twice == once
    assert len(twice["hooks"]["Stop"]) == 1


def test_merging_hooks_leaves_other_hooks_alone():
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "make lint"}]}], "PreToolUse": []}}
    merged = claude_code.merge_hooks(existing)
    assert "make lint" in json.dumps(merged["hooks"]["Stop"])
    assert claude_code.BELL_COMMAND in json.dumps(merged["hooks"]["Stop"])
    assert "PreToolUse" in merged["hooks"]


def test_removing_hooks_undoes_merging_exactly():
    assert claude_code.remove_hooks(claude_code.merge_hooks({})) == {}


def test_removing_hooks_leaves_other_hooks_alone():
    """The whole reason removal is a command: hand-editing is how a neighbour's hook dies."""
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "make lint"}]}]}}
    stripped = claude_code.remove_hooks(claude_code.merge_hooks(existing))
    assert stripped == existing


def test_removing_hooks_from_a_shared_entry_keeps_the_neighbour():
    """A single entry holding both commands must lose one hook, not the entry."""
    shared = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "make lint"}, {"type": "command", "command": "cairn bell"}]}
            ]
        }
    }
    stripped = claude_code.remove_hooks(shared)
    assert "make lint" in json.dumps(stripped)
    assert claude_code.BELL_COMMAND not in json.dumps(stripped)
    assert len(stripped["hooks"]["Stop"]) == 1


def test_removing_hooks_twice_is_a_no_op():
    once = claude_code.remove_hooks(claude_code.merge_hooks({}))
    assert claude_code.remove_hooks(once) == once


def test_removing_hooks_leaves_no_empty_husk_behind():
    """Nobody should have to wonder later what an empty `"hooks": {}` was for."""
    assert "hooks" not in claude_code.remove_hooks(claude_code.merge_hooks({"env": {"A": "1"}}))


def test_session_states_degrades_to_empty_rather_than_raising(tmp_path, monkeypatch):
    """The session registry is undocumented. A shape change must cost a nudge, not an outage."""
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "fine.json").write_text('{"pid": 1, "cwd": "/tmp", "status": "idle"}', encoding="utf-8")
    states = claude_code.session_states()
    assert [s["status"] for s in states] == ["idle"]


def test_session_states_is_empty_when_the_product_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path / "absent")
    assert claude_code.session_states() == []


# -- session_state: the reader the nudger injects ------------------------------
#
# Everything below guards one decision: whether it is safe to type into a live
# terminal. Only "idle" may ever come back as itself. Every other answer has to
# collapse to None, because the nudger treats None as "do not touch it".


def _publish(tmp_path, monkeypatch, workdir, **record):
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path)
    payload = {"pid": os.getpid(), "cwd": str(workdir), **record}
    (tmp_path / f"{payload['pid']}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("status", ["idle", "busy", "waiting"])
def test_a_known_status_from_a_live_process_is_reported(tmp_path, monkeypatch, status):
    work = tmp_path / "work"
    work.mkdir()
    _publish(tmp_path, monkeypatch, work, status=status)
    assert claude_code.session_state(work) == status


def test_a_stale_record_reports_nothing(tmp_path, monkeypatch):
    """These files outlive the process that wrote them.

    A crashed session leaves a record still saying `idle`. Typing into the pane
    it used to own is the worst thing this path can do, so a dead pid must not
    look wakeable.
    """
    work = tmp_path / "work"
    work.mkdir()
    _publish(tmp_path, monkeypatch, work, status="idle")
    monkeypatch.setattr(claude_code, "_alive", lambda _pid: False)
    assert claude_code.session_state(work) is None


def test_an_unrecognised_status_reports_nothing(tmp_path, monkeypatch):
    """The field is undocumented and may gain values. Unknown is never safe."""
    work = tmp_path / "work"
    work.mkdir()
    _publish(tmp_path, monkeypatch, work, status="compacting")
    assert claude_code.session_state(work) is None


@pytest.mark.parametrize("pid", [0, -1, "not-a-pid", None])
def test_an_unusable_pid_reports_nothing(tmp_path, monkeypatch, pid):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path)
    (tmp_path / "s.json").write_text(json.dumps({"pid": pid, "cwd": str(work), "status": "idle"}), encoding="utf-8")
    assert claude_code.session_state(work) is None


def test_one_directory_can_hold_several_sessions(tmp_path, monkeypatch):
    """Observed live: two records for one checkout, one busy and one silent."""
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path)
    for name, status in (("a", "idle"), ("b", None)):
        record = {"pid": os.getpid(), "cwd": "/w/shared", "name": name}
        if status:
            record["status"] = status
        (tmp_path / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
    assert len(claude_code.sessions_for_cwd(Path("/w/shared"))) == 2


def test_the_usable_session_wins_over_the_alphabetically_first(tmp_path, monkeypatch):
    """Which pane a nudge is typed into must not be decided by a filename."""
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path)
    (tmp_path / "aaa.json").write_text(
        json.dumps({"pid": os.getpid(), "cwd": "/w/shared", "name": "silent"}), encoding="utf-8"
    )
    (tmp_path / "zzz.json").write_text(
        json.dumps({"pid": os.getpid(), "cwd": "/w/shared", "name": "ready", "status": "idle"}), encoding="utf-8"
    )
    assert claude_code.session_for_cwd(Path("/w/shared"))["name"] == "ready"
    assert claude_code.session_state(Path("/w/shared")) == "idle"


def test_a_live_session_wins_over_a_dead_one_with_a_status(tmp_path, monkeypatch):
    """A stale record still says `idle`; liveness has to outrank it."""
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path)
    (tmp_path / "dead.json").write_text(
        json.dumps({"pid": 2**31 - 1, "cwd": "/w/shared", "name": "ghost", "status": "idle"}), encoding="utf-8"
    )
    (tmp_path / "live.json").write_text(
        json.dumps({"pid": os.getpid(), "cwd": "/w/shared", "name": "real", "status": "idle"}), encoding="utf-8"
    )
    assert claude_code.session_for_cwd(Path("/w/shared"))["name"] == "real"


def test_an_unknown_directory_reports_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "sessions_dir", lambda: tmp_path)
    assert claude_code.session_state(tmp_path / "nowhere") is None


def test_alive_agrees_with_reality_for_this_process():
    """One test that exercises the real check rather than a monkeypatch."""
    assert claude_code._alive(os.getpid()) is True
