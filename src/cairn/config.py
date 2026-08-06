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

**Whether the hub lets this machine in** is configuration, and the same value on
both sides of the wire: `token()` is what the hub requires and what the client
sends. Absent is the ordinary answer and means no authentication at all. It is
access control and never provenance — see `token`.

**What to run when the bell rings** is configuration too, and file-only on
purpose — no environment override. An env var would mean anything able to set
one in a session's environment could choose a command cairn then runs at every
turn boundary; a file under the operator's own config directory is a much
smaller door for a setting nobody changes per invocation. See `bell_command`.

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
import sys
import tomllib
from pathlib import Path

from cairn.errors import NameMoved, NotRegistered, UsageError

DEFAULT_HUB = "http://127.0.0.1:7777"


def config_path() -> Path:
    """Return the path to the config file, honouring XDG."""
    root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(root) / "cairn" / "config.toml"


def state_dir() -> Path:
    """Return the directory holding per-directory identity, honouring XDG."""
    root = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(root) / "cairn"


NOT_IN_EFFECT = "so none of it is in effect"
"""The clause every unusable-config error ends on.

Which key was being looked up is an accident of what ran first. What the reader
has to know is that the *whole file* was discarded, because the symptom they are
about to chase is a hub URL or a token reverting to a default that is nowhere in
the file they are staring at.
"""


def _file_config() -> dict[str, object]:
    """Return the parsed config file, or `{}` when there is not one.

    **A file that exists and cannot be used is an error, never silence.** That is
    the second time this module has had to learn the rule — `bell_command` raises
    for the same reason, that a config which quietly disables itself is
    indistinguishable from a quiet week for as long as nobody looks.

    Two ways in, both measured, and both arriving from a Windows shell appending
    a token:

    - **UTF-16**, which `>>` produces in Windows PowerShell 5.1. `UnicodeDecodeError`
      is a `ValueError`, so it was not caught here and `run()` deliberately does not
      catch it either: a traceback plus exit **1**, the code for "asked, nothing to
      report". That is the poisoned-mailbox shape, reached through a door nobody had
      thought to check.
    - **A UTF-8 BOM**, which PowerShell 5.1 writes for `-Encoding utf8`. Three bytes,
      and they raised `TOMLDecodeError`, which *was* caught — so the entire file was
      discarded in silence. `cairn config` then reported the localhost default while
      naming a file that said otherwise, and a hub whose token lived in that file
      **started open**, its banner missing `(token required)` and authenticating
      nobody. The quieter failure is the more dangerous one.

    A BOM is now simply consumed. `utf-8-sig` strips one if present and is byte for
    byte `utf-8` otherwise, which is the right treatment for a sequence that carries
    no meaning here and that no operator typed on purpose. Everything else is
    reported, named, and exits 3.
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        msg = (
            f"{path} is not valid UTF-8, {NOT_IN_EFFECT}. On Windows this is usually PowerShell's `>>`, "
            f"which writes UTF-16 — rewrite the file, and append with `Add-Content -Encoding ascii`."
        )
        raise UsageError(msg) from exc
    except OSError as exc:
        msg = f"{path} exists but could not be read ({exc.strerror or exc}), {NOT_IN_EFFECT}"
        raise UsageError(msg) from exc
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path} is not valid TOML ({exc}), {NOT_IN_EFFECT}"
        raise UsageError(msg) from exc


def hub_url(override: str | None = None) -> str:
    """Resolve the hub URL: flag, then CAIRN_HUB, then config file, then default."""
    if override:
        return override
    env = os.environ.get("CAIRN_HUB")
    if env:
        return env
    from_file = _file_config().get("hub")
    return str(from_file) if isinstance(from_file, str) and from_file else DEFAULT_HUB


def token() -> str | None:
    """Return the shared token this machine uses, or None if there is not one.

    `CAIRN_TOKEN`, then `token` in the config file, then nothing. **Both ends of
    the wire call this same function** — the hub decides whether to require a
    token with it, and the client decides whether to send one — which is what
    stops the two halves disagreeing about where the secret is kept. A hub and a
    client on one box therefore agree by construction.

    `None` is the ordinary answer and means the hub authenticates nobody, which
    is what every build before this one did. See `docs/design.md` §11 item 3 for
    what that costs and why it was accepted; the short version is that this is
    access control and nothing else. It says a request came from somebody holding
    the token. It says nothing whatever about *which agent* sent a message, and
    no provenance verdict may ever be derived from it — see invariant I1 and
    `provenance`'s docstring.

    **There is deliberately no `--token` flag**, and this is the opposite
    conclusion to `bell_command`'s for a different reason. A secret passed as an
    argument is in `/proc/<pid>/cmdline` and in every `ps` on that machine, for
    the whole life of a hub that runs for weeks. An environment variable is the
    documented way to give it to the container; the config file is for a machine
    where a human types commands.
    """
    return _resolve_token()[0]


ENV_SOURCE = "$CAIRN_TOKEN"
"""What `token_source` calls the environment. Printed, so it is written once."""

FILE_SOURCE = "config file"
"""What `token_source` calls the config file. Printed, so it is written once."""


def token_source() -> str:
    """Return where `token` got its answer, or `""` when there is no token.

    Exists because the failure this diagnoses is never "is there a token" — it is
    "I edited the file and something else was overriding it". Presence is the
    useless half of that; the source is the answer.

    Returns a label rather than the value, and no caller may be given the value:
    the whole point of a config surface is that it can be pasted into a message
    to whoever is helping, and a surface that prints the secret cannot be.
    """
    return _resolve_token()[1]


def _resolve_token() -> tuple[str | None, str]:
    """Resolve the token and name its source, in one place so the two cannot drift.

    `token` and `token_source` are two views of one decision. Written as two
    resolvers they would agree until somebody changed the precedence in one of
    them, and the symptom would be a diagnostic confidently naming the wrong
    source — worse than no diagnostic, because it is believed.
    """
    env = os.environ.get("CAIRN_TOKEN")
    if env:
        return env, ENV_SOURCE
    from_file = _file_config().get("token")
    if not isinstance(from_file, str) or not from_file:
        return None, ""
    _warn_if_readable(config_path())
    return from_file, FILE_SOURCE


_warned_about: set[Path] = set()

MODES_ARE_MEANINGFUL = os.name != "nt"
"""Whether this platform's `st_mode` says anything about who may read a file.

Windows synthesizes the whole mode from one read-only attribute — `0o666` for a
writable file, `0o444` for a read-only one — with no relation to the ACL that
actually governs access. So the check below is not merely unhelpful there: it
matches on **every** correctly-configured Windows machine, because `0o666 & 0o077`
is non-zero for any file anybody can write. And the fix it prescribes is `chmod`,
which Windows does not have.

A warning that is always on is a warning that says nothing, and this one would
also be wrong about the thing it named. So cairn declines to answer where it
cannot check — the same choice `provenance` makes about a signature it has no key
for. What it does *not* do is print "cannot check permissions here" on every
invocation, because a clause on every reading is the furniture
`docs/design.md` §12 item 18 measured. That sentence lives in
`docs/deployment.md`, where somebody setting a token up meets it once.

WSL reports `posix` and keeps the real check, which is the common case for a
Windows box in this fleet anyway.
"""


def _warn_if_readable(path: Path) -> None:
    """Say once if a file holding a secret is readable by anybody else.

    cairn chmods the one other secret it writes (`signing.key`, mode 0600) at
    creation. It cannot do that here: the config file is the operator's, it
    predates the token, and silently changing the mode of a file somebody else
    made is worse than saying so. So this warns and leaves it alone.

    Once per path per process, because the resolver is called on every hub
    request and a warning printed a thousand times is a warning nobody reads.

    Silent where a mode means nothing — see `MODES_ARE_MEANINGFUL`.
    """
    if not MODES_ARE_MEANINGFUL or path in _warned_about:
        return
    _warned_about.add(path)
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        print(
            f"cairn: warning: {path} holds a token and is readable by others "
            f"(mode {mode & 0o777:03o}). Run: chmod 600 {path}",
            file=sys.stderr,
        )


def bell_command() -> list[str] | None:
    """Return the command the operator wants run when the bell rings, if any.

    Three answers, not two. `None` means the key is absent, which is the normal
    case and says nothing is wrong. A list means it is usable. Anything else
    **raises**, and that is the decision worth defending: this feature's hook
    path is silent by construction — `notify.fire` detaches and `cli.cmd_bell`
    may not fail loudly — so a config file that silently disabled itself would be
    indistinguishable from a quiet week for as long as nobody looked. The
    exception is a `CairnError`, so `cmd_bell` swallows it exactly as it swallows
    everything else, and `cairn bell --test` is where an operator meets it.

    An argv list rather than a shell string, and `notify.PLACEHOLDERS` has why.
    """
    value = _file_config().get("bell_command")
    if value is None:
        return None
    if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
        return [str(part) for part in value]
    shape = "an empty list" if value == [] else f"a {type(value).__name__}"
    msg = (
        f"bell_command in {config_path()} must be a non-empty list of strings, but it is {shape}. "
        'A shell string is not accepted: write ["sh", "-c", "…"] if that is what you want.'
    )
    raise UsageError(msg)


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


def key_file(cwd: Path | None = None) -> Path:
    """Return where this directory's signing key lives.

    Public where `_identity_file` and `_pin_file` are private, because `signing`
    is a different module and the slug scheme is the thing that must not be
    reinvented: three kinds of per-directory state that disagreed about how a
    path becomes a filename would be three kinds that silently stop matching
    after a symlink or a rename. `signing` owns the key; this file owns where
    per-directory state goes.
    """
    return state_dir() / "keys" / f"{_slug(cwd or Path.cwd())}.json"


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
    r"""Return the identity a name is pinned to: where the holder actually is.

    `(machine, cwd)` rather than a session id, because a session id is optional
    — a product that publishes none leaves it empty — while these two are always
    populated, already travel on the wire, and are exactly the pair that a
    session restarting in place holds fixed.

    **The colon is a separator that does not separate, and half this fleet proves
    it.** A Windows `cwd` carries its own — the real value on one peer machine
    produces `HID4258W:C:\Users\…`, so `split(":")` yields three parts and the
    second is `C`. Nothing splits it today: the string is written whole, compared
    whole with `==`, and shown whole in `NameMoved`, which is why this has never
    failed. It is recorded here because the format *reads* like it can be parsed,
    and the first person to parse it will be right about the shape and wrong
    about the fleet. If a caller ever needs the two halves back, store the pair
    rather than teaching this string a quoting rule.
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
        f'hub = "{hub}"\n'
        "\n"
        "# Optional: the shared token this hub requires. Absent means the hub\n"
        "# authenticates nobody, which is what every build before 0.3.0 did.\n"
        "# It is access control and not proof of who sent anything -- messages stay\n"
        "# UNVERIFIED either way. $CAIRN_TOKEN overrides this; there is no flag,\n"
        "# because an argument is visible in `ps`. chmod 600 this file if you use it.\n"
        '# token = "..."\n'
        "\n"
        "# Optional: a command to run when the bell rings, so a human who is not at\n"
        "# this machine hears it. An argv list, never a shell string. It carries a\n"
        "# count and a name and never a message body. Check it with `cairn bell --test`.\n"
        '# bell_command = ["notify-send", "cairn", "{reason}"]\n'
        "#\n"
        "# Placeholders: {count} {agent} {reason}\n"
        "# Also passed as: CAIRN_BELL_COUNT / CAIRN_BELL_AGENT / CAIRN_BELL_REASON\n"
        "# A shell is available if you ask for one, and then it is visible here:\n"
        '# bell_command = ["sh", "-c", "curl -sS -d \\"$CAIRN_BELL_REASON\\" https://example/…"]\n',
        encoding="utf-8",
    )
    return path
