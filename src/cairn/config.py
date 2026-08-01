"""Where the hub is, and who this session is.

Two separate questions with two separate answers.

**Which hub** is configuration: flag, then `CAIRN_HUB`, then the config file,
then a localhost default. Ordinary precedence, stated out loud so nobody has to
read code to predict it.

**Which agent I am** is state, not configuration, and it is keyed by working
directory. `cairn register bench/firmware` writes that name under the cwd, and
every later command in that directory knows who it is without being told again.
A session that restarts in the same directory picks its identity back up, which
is exactly what should happen — the cursor on the hub is keyed by name, so
recovering the name recovers the backlog.

Known limit, stated rather than papered over: two sessions in the *same*
directory would share one identity. Set `CAIRN_AGENT` in one of them.

**Which peers I have talked to** is also state, also keyed by working directory.
A name is an address and re-registering one is how a restarted session recovers
its mail, so nothing on the network distinguishes "the same session came back"
from "something else took the name". The pin file records what each name reached
the first time this directory sent to it, and `check_pin` refuses when that
changes. Registering once per directory is the normal case and costs nothing
here — the pin only ever fires when a name genuinely moves.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from cairn.errors import NameMoved, NotRegistered

DEFAULT_HUB = "http://127.0.0.1:7777"


def config_path() -> Path:
    """Return the path to the config file, honouring XDG."""
    root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(root) / "cairn" / "config.toml"


def state_dir() -> Path:
    """Return the directory holding per-directory identity, honouring XDG."""
    root = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(root) / "cairn"


def _file_config() -> dict[str, object]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def hub_url(override: str | None = None) -> str:
    """Resolve the hub URL: flag, then CAIRN_HUB, then config file, then default."""
    if override:
        return override
    env = os.environ.get("CAIRN_HUB")
    if env:
        return env
    from_file = _file_config().get("hub")
    return str(from_file) if isinstance(from_file, str) and from_file else DEFAULT_HUB


def _slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(path.resolve())).strip("-").lower() or "root"


def _identity_file(cwd: Path | None = None) -> Path:
    return state_dir() / "identity" / f"{_slug(cwd or Path.cwd())}.json"


def remember_identity(name: str, cwd: Path | None = None) -> Path:
    """Record that this working directory belongs to agent `name`."""
    path = _identity_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name}), encoding="utf-8")
    return path


def current_identity(cwd: Path | None = None) -> str | None:
    """Return this session's agent name, or None if it has not registered."""
    env = os.environ.get("CAIRN_AGENT")
    if env:
        return env
    path = _identity_file(cwd)
    if not path.is_file():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("name")) or None
    except (OSError, json.JSONDecodeError):
        return None


def require_identity(cwd: Path | None = None) -> str:
    """Return this session's agent name, or raise `NotRegistered`."""
    name = current_identity(cwd)
    if not name:
        detail = f"no identity recorded for {cwd or Path.cwd()}"
        raise NotRegistered(detail)
    return name


def _pin_file(cwd: Path | None = None) -> Path:
    return state_dir() / "pins" / f"{_slug(cwd or Path.cwd())}.json"


def _read_pins(cwd: Path | None = None) -> dict[str, str]:
    path = _pin_file(cwd)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


def _write_pins(pins: dict[str, str], cwd: Path | None = None) -> None:
    path = _pin_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pins, indent=2, sort_keys=True), encoding="utf-8")


def pin_of(machine: str, peer_cwd: str) -> str:
    """Return the identity a name is pinned to: where the holder actually is.

    `(machine, cwd)` rather than a session id, because a session id is optional
    — a product that publishes none leaves it empty — while these two are always
    populated, already travel on the wire, and are exactly the pair that a
    session restarting in place holds fixed.
    """
    return f"{machine}:{peer_cwd}"


def check_pin(name: str, machine: str, peer_cwd: str, cwd: Path | None = None) -> None:
    """Remember what `name` reaches, or refuse because it has changed.

    A name is an address, and re-registering one is how a restarted session gets
    its mail back — so nothing on the network distinguishes "the same session
    came back" from "something else took the name". The hub stops a newcomer
    inheriting a predecessor's unread mail; this stops a sender delivering *new*
    mail to a stranger.

    The pin is per sending directory and is recorded on first use, so it costs
    nothing until a name actually moves. Raising rather than warning is
    deliberate: a warning about a message that was sent anyway is not a
    safeguard, it is a note in a log nobody reads.
    """
    pins = _read_pins(cwd)
    current = pin_of(machine, peer_cwd)
    previous = pins.get(name)
    if previous and previous != current:
        raise NameMoved(name, previous, current)
    if previous != current:
        pins[name] = current
        _write_pins(pins, cwd)


def forget_pin(name: str, cwd: Path | None = None) -> bool:
    """Drop the pin for `name`, so the next send re-learns it. True if there was one."""
    pins = _read_pins(cwd)
    if pins.pop(name, None) is None:
        return False
    _write_pins(pins, cwd)
    return True


def write_default_config(hub: str = DEFAULT_HUB) -> Path:
    """Write an annotated config file and return its path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# cairn configuration.\n"
        "# Precedence: --hub flag, then $CAIRN_HUB, then this file, then the default.\n"
        f'hub = "{hub}"\n',
        encoding="utf-8",
    )
    return path
