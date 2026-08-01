"""Argument parsing, subcommand dispatch, exit codes.

No business rules live here. Each subcommand resolves configuration, calls one
or two client methods, hands the result to `render`, and picks an exit code.

Exit codes, which a script may rely on:

    0    it worked
    1    it worked, and the answer is "nothing" — empty inbox, no peers
    2    the hub could not be reached
    3    the command cannot be carried out as asked
    130  interrupted

`1` and `2` mean opposite things and must never be collapsed. "No mail" is an
answer; "no hub" is a broken pipe.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from cairn import __version__, config, nudge, provenance, render, skill
from cairn.client import HubClient
from cairn.errors import CairnError, UsageError
from cairn.wire import BROADCAST, Agent, Artifact, InboxEntry, WireError, normalize_subject

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_NOTHING = 1


def _hub(args: argparse.Namespace) -> str:
    """Resolve which hub this command talks to.

    Named separately from `_client` because the URL is also *output*: every
    answer of "nothing" prints it, so the reader can tell a quiet network from a
    misconfigured one without a second command.
    """
    return config.hub_url(getattr(args, "hub", None))


def _client(args: argparse.Namespace) -> HubClient:
    return HubClient(_hub(args))


def _check_recipient(client: HubClient, recipient: str) -> None:
    """Refuse to send if this name has moved since this directory last used it.

    Costs one lookup per send. That is the price of not silently handing a
    colleague's mail to whoever holds the name now, and it is paid on a command
    that is already talking to the hub.

    Broadcast is exempt: `*` has no holder to move. An unknown recipient is left
    alone too — the hub rejects it with a better message than this could.
    """
    if recipient == BROADCAST:
        return
    match = next((a for a in client.peers() if a.name == recipient), None)
    if match is not None:
        config.check_pin(recipient, match.machine, match.cwd)


def _subject(raw: str) -> str:
    """Fold a subject locally, and make a bad one exit 3 rather than traceback.

    `wire.normalize_subject` raises `WireError`, which is a `ValueError` and so
    is deliberately *not* caught by `run()`. Left alone that means a mistyped
    subject prints a stack trace and exits 1 — the code for "asked, nothing to
    report" — which is the same shape as the poisoned mailbox docs/design.md
    §12 item 3 records, arriving through a different door. A malformed argument
    is exit 3, so the conversion happens at the boundary where the argument
    arrives.
    """
    try:
        return normalize_subject(raw)
    except WireError as exc:
        raise UsageError(str(exc)) from exc


def _artifacts(specs: Sequence[str]) -> list[Artifact]:
    """Parse `HOST:PATH` pairs, warning about a path nobody else can follow.

    Warned about rather than refused, because cairn never resolves a path and
    the filesystem it names is usually not this one — a rule this build has no
    standing to enforce is I3 again. What it can do is report the two things it
    is actually able to check, and both were found by live sessions.

    **A relative path is not a location.** It is meaningless the moment it leaves
    the shell that produced it, and an artifact on a *note* is read months later
    by somebody with no idea what the writer's working directory was.

    **A path that is not here may already be broken.** The harder case, and the
    one the first warning walked straight past: a session wrote
    `bench:/srv/hil/441/n33-coldstart.ctf` into an append-only note and later
    found the file unreachable from its own machine. Absolute, well-formed,
    stored in silence, permanent. So a path that does not exist locally is said
    out loud — with the condition attached, because the ordinary cross-machine
    reference is a path that legitimately is not here. That phrasing matters: the
    check cannot tell the two apart, so it must not pretend to.
    """
    artifacts = []
    for spec in specs:
        host, sep, path = spec.partition(":")
        if not sep or not path:
            msg = f"artifact {spec!r} must look like HOST:/absolute/path — HOST is always required"
            raise UsageError(msg)
        if not path.startswith("/"):
            print(
                f"cairn: warning: artifact path {path!r} is not absolute, so it names nothing on {host}",
                file=sys.stderr,
            )
        elif not Path(path).exists():
            print(
                f"cairn: note: {path} is not on this machine — fine if {host} is somewhere else, "
                f"already broken if {host} is here",
                file=sys.stderr,
            )
        artifacts.append(Artifact(host=host, path=path))
    return artifacts


# -- commands -----------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    """Join the network under a name."""
    from cairn.adapters import default

    adapter = default()
    agent = Agent(
        name=args.name,
        machine=args.machine or socket.gethostname(),
        cwd=str(Path.cwd()),
        capabilities=tuple(args.capability),
        session_id=adapter.session_id(),
    )
    client = _client(args)
    registration = client.register(agent)
    joined = registration.agent
    config.remember_identity(joined.name)
    print(f"registered as {joined.name} on {joined.machine}")
    print(f"  cwd          {joined.cwd}")
    print(f"  capabilities {', '.join(joined.capabilities) or '—'}")
    print(render.arrival_note(registration), end="")
    print(_open_questions(client), end="")
    return 0


def _open_questions(client: HubClient) -> str:
    """Return the "something is unanswered" line, or nothing.

    Wrapped in its own catch, and that is not defensive noise. `/v1/subjects`
    does not exist on a hub built before this cut, and `client._call` maps a 404
    to `Unreachable` — so without this, registering against an older hub would
    fail outright with exit 2 on a hub that is up, healthy, and perfectly able to
    carry messages. Additive routes are only additive if the caller treats their
    absence as "no answer" rather than as an outage.

    `WireError` is caught alongside, and only here. It is a `ValueError`, so
    `run()` lets it become a traceback — correct for a real read, where a hub
    speaking nonsense is something the caller must be told about. This is a
    courtesy line on a command whose actual job already succeeded; failing a
    registration over the garnish would be the tail wagging the dog.
    """
    try:
        return render.open_questions_hint(client.subjects())
    except (CairnError, WireError):
        return ""


def cmd_whoami(args: argparse.Namespace) -> int:
    """Print this session's identity."""
    name = config.current_identity()
    if not name:
        print("not registered in this directory; run `cairn register <name>`")
        return EXIT_NOTHING
    print(name)
    _ = args
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    """List the other agents on the network, optionally only those that can do a thing.

    The filter is client-side and needs no route of its own. It exists because
    the skill sells capabilities as *how you find the machine that has the thing
    you need*, and then offered no way to ask — a live session went looking for
    it, did not find it, and read the whole list by eye. Three agents is fine by
    eye; the promise stops being true well before thirty.

    Matching is exact against the strings agents registered. Those strings are
    unverified assertions — a session registering `-c hil` is claiming, not
    proving, and one live session advertised hardware capabilities it turned out
    not to have. I3: this narrows a list, it does not certify anyone.
    """
    hub = _hub(args)
    everyone = HubClient(hub).peers(exclude=config.current_identity())
    agents = everyone
    if args.capability:
        wanted = set(args.capability)
        agents = [a for a in everyone if wanted <= set(a.capabilities)]
    text = render.peers_text(agents, hub, wanted=args.capability, registered=len(everyone))
    print(render.peers_json(agents) if args.json else text, end="")
    return 0 if agents else EXIT_NOTHING


def cmd_tell(args: argparse.Namespace) -> int:
    """Send a message that needs no answer.

    A broadcast reports how far it went. `sent seq 1 to *` is the same line on a
    hub with twelve agents and on a hub with none, and the whole point of a
    broadcast is discovery — a live session used one to announce a capability,
    and could only infer that anybody had heard by getting a reply. The count
    costs one lookup, and only on `*`.
    """
    me = config.require_identity()
    client = _client(args)
    _check_recipient(client, args.recipient)
    message = client.send("tell", me, args.recipient, args.body, artifacts=_artifacts(args.artifact))
    print(f"sent seq {message.seq} to {message.recipient}{_reach(client, message.recipient, me)}")
    return 0


def _reach(client: HubClient, recipient: str, me: str) -> str:
    """Say how many mailboxes a broadcast landed in, or nothing at all.

    Guarded like the other garnishes: the message is already stored, so a lookup
    that fails must cost the count and not the send. Zero is worth saying out
    loud — it is the shape of "you are the only one here", which on a two-machine
    tool is more often a misconfiguration than a fact.
    """
    if recipient != BROADCAST:
        return ""
    try:
        others = len(client.peers(exclude=me))
    except (CairnError, WireError):
        return ""
    return f" · {others} other agent{'' if others == 1 else 's'} registered"


def cmd_ask(args: argparse.Namespace) -> int:
    """Send a message that expects an answer.

    This assigns a correlation id and delivers. The answer arrives in your inbox
    like any other message; `cairn inbox --wait` is how to stand still for it.
    """
    me = config.require_identity()
    correlation = args.correlation or f"q-{uuid.uuid4().hex[:8]}"
    client = _client(args)
    _check_recipient(client, args.recipient)
    message = client.send(
        "ask", me, args.recipient, args.body, correlation_id=correlation, artifacts=_artifacts(args.artifact)
    )
    print(f"asked seq {message.seq} of {message.recipient}, correlation {correlation}")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    """Answer an `ask`, with references to anything too big to say.

    `-a` was missing here until a peer session tried to use it and got
    `unrecognized arguments`. It had read the skill's rule — big things go behind
    a path — as the universal rule it is written as, and folded the path into its
    prose instead. Of the three sends this is the one that most needs it: an
    answer is what you produce *after* doing the work somebody asked for, and the
    work is usually a file.
    """
    me = config.require_identity()
    client = _client(args)
    _check_recipient(client, args.recipient)
    message = client.send(
        "reply", me, args.recipient, args.body, correlation_id=args.correlation, artifacts=_artifacts(args.artifact)
    )
    print(f"replied seq {message.seq} to {message.recipient} for {args.correlation}")
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    """Read unread messages, and by default mark them read.

    `--wait` does not change what reading means. The ordinary read happens
    first, and only an empty one blocks — so a question already answered by the
    time this runs is answered immediately, which is the first of the
    constraints docs/design.md §12 item 3 records. What arrives is printed and
    acked exactly as it would have been without the flag: there is no partial
    ack, because there is no second code path for one to live in.
    """
    # Finite, not merely positive. `float("infinity")` and `nan` both survive a
    # `> 0` test, and `inf` reaches `socket.settimeout`, which raises
    # `OverflowError` — not an `OSError`, so `client.stream` does not convert it
    # and `run()` deliberately does not catch it. That is a traceback plus exit
    # 1, the code for "nothing to report": the same shape as the poisoned inbox
    # this cut fixed in `store.append`, arriving by a different door.
    if args.wait is not None and not (0 < args.wait < math.inf):
        msg = f"--wait needs a positive, finite number of seconds, got {args.wait:g}"
        raise UsageError(msg)
    me = config.require_identity()
    client = _client(args)
    if args.wait is None:
        messages = client.inbox(me, limit=args.limit)
    else:
        from cairn import waiting

        messages = waiting.wait_for_mail(client, me, timeout=args.wait, limit=args.limit)
    entries = [InboxEntry(message=m, provenance=provenance.assess(m)) for m in messages]
    # flush before the ack, not after. stdout is block-buffered off a tty and
    # nothing here handles SIGTERM, so a host killing the command in the window
    # between these two lines would move the cursor past mail that never reached
    # the terminal — the one way this command can lose a message outright.
    print(render.inbox_json(entries) if args.json else render.inbox_text(entries, _hub(args)), end="", flush=True)
    if not messages:
        if args.wait is not None:
            print(f"cairn: waited {args.wait:g}s, still nothing.", file=sys.stderr)
        return EXIT_NOTHING
    if not args.no_ack:
        client.ack(me, max(m.seq for m in messages))
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    """Leave something on a subject for whoever turns up next.

    Prints the subject the hub filed it under rather than the one that was
    typed, and says so when the two differ. Subjects are case-folded so that
    `rig-a` and `Rig-A` cannot become two piles — but a fold nobody is told about
    is the same silent surprise in a smaller font.

    It also says whether the pile is one that already existed. Folding case only
    catches the split it was designed for; a live session pointed out the bigger
    one it does not touch — `soak-441`, `eval-441`, `run-441` and `441` are four
    subjects cairn will happily create, and creating one looks exactly like
    adding to one. So the write reports which happened, and a new pile says so.
    """
    me = config.require_identity()
    client = _client(args)
    note = client.write_note(
        author=me,
        body=args.body,
        subject=_subject(args.subject),
        question=args.question,
        artifacts=_artifacts(args.artifact),
    )
    print(f"{'question' if note.question else 'note'} {note.id} on {note.subject}{_pile(client, note.subject)}")
    if note.subject != args.subject:
        print(f"  subject folded from {args.subject!r}")
    if note.question:
        print(f'  it stays open until someone runs `cairn settle {note.id} "…"` — including after you are gone')
    return 0


def _pile(client: HubClient, subject: str) -> str:
    """Say whether that subject already existed, and how much is on it now.

    Counted from the index, which groups by exact subject, so writing to `rig-a`
    when only `rig-a/chamber` has notes still reads as new — which it is.

    Guarded like `_open_questions`, and for the same reason: this is a garnish on
    a write that already succeeded, and an older hub without the route must not
    turn a stored note into a failed command.

    It costs a second round trip, and fetches the whole index to read one row.
    Accepted for now because notes are written a handful of times a shift and the
    index is one row per subject. The cheaper shape is the write response
    carrying the count — that is a sibling key next to `note`, exactly as
    `Registration` gained its own, and worth doing if the index ever grows past
    a screenful or if a caller starts writing notes in a loop.
    """
    try:
        match = next((s for s in client.subjects() if s.subject == subject), None)
    except (CairnError, WireError):
        return ""
    if match is None or match.notes <= 1:
        return " · new subject — `cairn notes` lists the ones that already exist"
    return f" · {match.notes} notes there now"


def cmd_settle(args: argparse.Namespace) -> int:
    """Answer an open question, whoever asked it.

    No ownership check, deliberately: the whole reason this exists is a session
    that ended and took its questions with it, leaving a peer holding the answer.
    See invariant I3 — cairn declares intent and does not enforce it.

    The subject is not an argument. It comes from the question being settled, so
    an answer cannot be filed away from its question.
    """
    me = config.require_identity()
    note = _client(args).write_note(author=me, body=args.body, settles=args.id, artifacts=_artifacts(args.artifact))
    print(f"note {note.id} on {note.subject} settles question {args.id}")
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    """Read sediment: one subject, everything unanswered, or the index.

    Reading a note consumes nothing. There is no cursor here and no ack — a pile
    is not a queue, and the next reader must find it exactly as this one did.

    With no subject and no filter this prints the index rather than every note
    ever written, because the useful question on arrival is "is there anything
    here", not "show me everything".

    The subject is folded to its canonical form *here*, before the call, so that
    the header names the pile that was actually searched. Sending the raw text
    and printing the raw text agrees with itself and disagrees with the hub —
    `cairn notes RIG-A` would head its output `RIG-A` while reading `rig-a`.
    """
    # A non-positive limit is refused rather than clamped, because both ways of
    # getting it wrong lie. SQLite reads `LIMIT -1` as "no limit", so a negative
    # number silently returns everything; `LIMIT 0` returns nothing, and an empty
    # page renders as "nothing on rig-a yet" — a pile of five notes reported as an
    # empty subject, which is the one answer a reader will act on without checking.
    if args.limit < 1:
        msg = f"--limit needs to be at least 1, got {args.limit}"
        raise UsageError(msg)
    client = _client(args)
    subject = _subject(args.subject) if args.subject is not None else None
    if subject is None and not args.open and not args.find:
        summaries = client.subjects()
        print(render.subjects_json(summaries) if args.json else render.subjects_text(summaries, _hub(args)), end="")
        return 0 if summaries else EXIT_NOTHING
    entries, total = client.notes(subject, open_only=args.open, find=args.find, limit=args.limit)
    read = [e.checked(provenance.assess_note(e.note)) for e in entries]
    scope = {"subject": subject, "open_only": args.open, "find": args.find}
    # Only the text renderer names the hub. The "nothing" answers say who they
    # asked because a *model* reads them and may have no idea what this
    # directory is configured against; whatever invoked `--json` chose the hub
    # itself one call ago, so telling it would be telling it its own argument.
    text = (
        render.notes_json(read, total, **scope)
        if args.json
        else render.notes_text(read, total, hub=_hub(args), **scope)
    )
    print(text, end="")
    return 0 if read else EXIT_NOTHING


def cmd_ack(args: argparse.Namespace) -> int:
    """Move the read cursor by hand, forward or — when asked — back."""
    cursor = _client(args).ack(config.require_identity(), args.seq, rewind=args.rewind)
    print(f"cursor at {cursor}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Drop this directory's pin for a name, so the next send re-learns it."""
    if config.forget_pin(args.name):
        print(f"forgot where {args.name} was; the next send will learn it again")
        return 0
    print(f"no pin recorded for {args.name} in this directory")
    return EXIT_NOTHING


def cmd_bell(args: argparse.Namespace) -> int:
    """Emit a turn-boundary bell for whichever agent product invoked us.

    Three properties matter more than anything this function does.

    **It never carries content.** The bell says how many messages are waiting
    and how to read them. Peer text in a hook is unattributable and gets treated
    as an injection attempt, correctly.

    **It never rings twice for the same mail.** If the reader chose not to open
    the inbox, that is the reader's call; ringing again every turn would be a
    loop, not a reminder.

    **It never fails loudly.** A hook that errors degrades the session it is
    attached to, so an unreachable hub means an empty response and exit 0.

    It reads a local counter when a nudger is maintaining one, and asks the hub
    only when nobody is. This runs at every single turn boundary, so the common
    case has to cost a `stat` and a small read rather than a round trip — but
    "the counter says zero" is only worth believing while something is still
    writing it, which is what the freshness check is for.
    """
    try:
        me = config.current_identity()
        if not me:
            print("{}")
            return 0
        if nudge.counter_is_fresh(me):
            count, head = nudge.read_unread(me)
        else:
            messages = _client(args).inbox(me, limit=args.limit)
            count, head = len(messages), max((m.seq for m in messages), default=0)
        if not count or head <= nudge.read_belled(me):
            print("{}")
            return 0
        nudge.latch_belled(me, head)
        # ensure_ascii=False so the reason reads as itself in the hook log rather
        # than as \uXXXX escapes; hook stdout is UTF-8 and render owns the wording.
        print(json.dumps({"decision": "block", "reason": render.bell_reason(count)}, ensure_ascii=False))
    except (CairnError, OSError, ValueError):
        print("{}")
    return 0


def cmd_nudge(args: argparse.Namespace) -> int:
    """Run the optional per-machine nudger in the foreground.

    Everything cairn does works without this. What it adds is that a peer whose
    human has walked away still hears the doorbell: the daemon keeps the local
    unread counter warm and, when a watched session is sitting idle, types one
    line into its terminal.

    One line, and never the message. See invariant I1 — a nudge that carried
    peer text would be indistinguishable from the human typing it.
    """
    from cairn.adapters import default

    adapter = default()
    watches = _watches(args)
    if not watches:
        msg = "nothing to watch; pass --watch NAME:/path, or run this from a registered directory"
        raise UsageError(msg)
    for watch in watches:
        # flush, for the same reason the hub's banner does: stdout is
        # block-buffered off a tty, so under nohup or a unit file this is the
        # only "what am I watching" record and it would otherwise never appear.
        print(f"watching {watch.agent} at {watch.cwd}", flush=True)
    nudge.run(
        config.hub_url(args.hub),
        watches,
        state_reader=adapter.session_state,
        poll_interval=args.poll_interval,
    )
    return 0


def _watches(args: argparse.Namespace) -> list[nudge.Watch]:
    """Build the watch list from `--watch` flags, or from this directory's identity."""
    from cairn.adapters import default

    if args.watch:
        watches = []
        for spec in args.watch:
            name, sep, path = spec.partition(":")
            if not sep or not path:
                msg = f"watch {spec!r} must look like AGENT:/path/to/working/directory"
                raise UsageError(msg)
            watches.append(nudge.Watch(agent=name, cwd=Path(path).expanduser()))
    else:
        me = config.current_identity()
        watches = [nudge.Watch(agent=me, cwd=Path.cwd())] if me else []
    # The pid is only ever used to find a terminal, so a session the product does
    # not report simply cannot be woken — the counter still gets maintained.
    adapter = default()
    resolved = []
    for watch in watches:
        candidates = adapter.sessions_for_cwd(watch.cwd)
        record = adapter.session_for_cwd(watch.cwd)
        if len(candidates) > 1:
            # Observed on a working machine, so this is a real branch rather than
            # defensive noise. The adapter picks the most usable one; which pane
            # gets typed into is still a guess, and a guess should be audible.
            named = ", ".join(f"{c.get('name') or '?'}(pid {c.get('pid')})" for c in candidates)
            chosen = record.get("name") if isinstance(record, dict) else "?"
            print(f"warning: {len(candidates)} sessions in {watch.cwd}: {named} — waking {chosen}", flush=True)
        pid = record.get("pid") if isinstance(record, dict) else None
        resolved.append(nudge.Watch(agent=watch.agent, cwd=watch.cwd, pid=int(pid) if pid else None))
    return resolved


def cmd_hub(args: argparse.Namespace) -> int:
    """Run the hub in the foreground."""
    from cairn.hub import serve
    from cairn.store import SqliteStore

    db = Path(args.db).expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    serve(SqliteStore(db), host=args.host, port=args.port)
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    """Install the bundled skill."""
    from cairn.adapters import default

    target = skill.install_skill(default().skills_dir())
    print(f"skill installed at {target}")
    _ = args
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    """Add or remove the turn-boundary bell in the host product's settings.

    This is the only file cairn writes that the user owns and shares with other
    tools, which is why removal is a command rather than a paragraph telling
    someone to edit JSON — that paragraph is how a neighbour's hook gets deleted
    by accident. The previous file is always saved alongside first.
    """
    from cairn.adapters import default

    adapter = default()
    settings_file = adapter.settings_path()
    settings = {}
    if settings_file.is_file():
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        settings_file.with_suffix(".json.cairn-backup").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    updated = adapter.remove_hooks(settings) if args.remove else adapter.merge_hooks(settings)
    verb = "removed from" if args.remove else "installed in"
    if updated == settings:
        print(f"hooks already {'absent from' if args.remove else 'present in'} {settings_file}; nothing to do")
        return 0
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    print(f"hooks {verb} {settings_file} (previous file saved alongside)")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show or create the config file."""
    if args.init:
        print(f"wrote {config.write_default_config(config.hub_url(args.hub))}")
        return 0
    print(f"hub          {config.hub_url(args.hub)}")
    print(f"config file  {config.config_path()}")
    print(f"state dir    {config.state_dir()}")
    print(f"identity     {config.current_identity() or '—'}")
    return 0


# -- parser -------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """An `ArgumentParser` whose usage errors land on cairn's exit code, not argparse's.

    argparse exits **2** on a bad command line, and 2 is cairn's "the hub could
    not be reached". So a typo reported itself as a network outage: measured on a
    peer session that ran `cairn reply … -a HOST:PATH` before `reply` accepted
    `-a`, got `unrecognized arguments`, and spent a moment wondering whether the
    hub had gone. A script doing `cairn reply … || echo "hub down"` would have
    said so out loud and been wrong.

    "You asked for something that cannot be carried out" is exit 3, and that is
    what a malformed command line is. Only `error()` is remapped — `--help` and
    `--version` go through `exit()`, which is untouched and still leaves 0.
    """

    def error(self, message: str) -> NoReturn:
        """Raise instead of exiting, so `run()` assigns the code."""
        detail = f"{message} (try `{self.prog} --help`)"
        raise UsageError(detail)


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - one flat statement per flag; splitting it hides the surface
    """Build the argument parser."""
    parser = _Parser(prog="cairn", description="Cross-machine messaging for coding agent sessions.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    parser.add_argument("--hub", help="hub URL; overrides $CAIRN_HUB and the config file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="join the network under a name")
    p.add_argument("name", help="unique address, e.g. bench/firmware")
    p.add_argument("-c", "--capability", action="append", default=[], help="repeatable, e.g. -c matlab -c hil")
    p.add_argument("--machine", help="defaults to the hostname")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("whoami", help="print this session's identity")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("peers", help="list the other agents")
    p.add_argument(
        "-c",
        "--capability",
        action="append",
        default=[],
        help="repeatable; show only agents claiming all of these, e.g. -c gpu",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_peers)

    p = sub.add_parser("tell", help="send a message that needs no answer")
    p.add_argument("recipient", help=f"an agent name, or {BROADCAST!r} for everyone")
    p.add_argument("body")
    p.add_argument("-a", "--artifact", action="append", default=[], metavar="HOST:PATH")
    p.set_defaults(func=cmd_tell)

    p = sub.add_parser(
        "ask",
        help="send a message that expects an answer",
        description=(
            "Send a message that expects an answer. This assigns a correlation id and "
            "delivers; the answer arrives in your inbox like any other message, and "
            "`cairn inbox --wait` is how to stand still for it. Said here rather than on "
            "every send: it is true once, and printing it each time costs the reader more "
            "than it tells them."
        ),
    )
    p.add_argument("recipient")
    p.add_argument("body")
    p.add_argument("--correlation", help="reuse an existing correlation id")
    p.add_argument("-a", "--artifact", action="append", default=[], metavar="HOST:PATH")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("reply", help="answer an ask")
    p.add_argument("recipient")
    p.add_argument("correlation")
    p.add_argument("body")
    p.add_argument("-a", "--artifact", action="append", default=[], metavar="HOST:PATH")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("inbox", help="read unread messages")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--no-ack", action="store_true", help="read without marking as read")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--wait",
        nargs="?",
        type=float,
        const=60.0,
        default=None,
        metavar="SECONDS",
        help="if the inbox is empty, block this long for something to arrive (default 60)",
    )
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser(
        "note",
        help="leave something on a subject for whoever turns up next",
        description=(
            "Leave a fact, a decision or an open question on a subject — a rig, a run, a board. "
            "A note has no recipient, rings no bell and is never consumed by reading: it waits at "
            "the subject for whoever turns up next, including sessions that do not exist yet."
        ),
    )
    p.add_argument("subject", help="the thing this is about, e.g. rig-a or eval-441")
    p.add_argument("body")
    p.add_argument(
        "-q",
        "--question",
        action="store_true",
        help="record this as an open loop; it stays open until someone runs `cairn settle`",
    )
    p.add_argument("-a", "--artifact", action="append", default=[], metavar="HOST:PATH")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("settle", help="answer an open question, whoever asked it")
    p.add_argument("id", type=int, help="the note id printed by `cairn notes`")
    p.add_argument("body")
    p.add_argument("-a", "--artifact", action="append", default=[], metavar="HOST:PATH")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser(
        "notes",
        help="read notes: one subject, everything unanswered, or the index",
        description=(
            "Read sediment. With no subject this prints the index of subjects and how much is "
            "unanswered on each. Reading consumes nothing — there is no cursor here, so the next "
            "reader finds everything you found."
        ),
    )
    p.add_argument("subject", nargs="?", help="omit to see the index of subjects")
    p.add_argument("--open", action="store_true", help="only questions nobody has settled")
    p.add_argument("--find", metavar="TEXT", help="substring search across bodies and subjects")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("forget", help="drop this directory's pin for a name that has legitimately moved")
    p.add_argument("name")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("ack", help="move the read cursor by hand")
    p.add_argument("seq", type=int)
    p.add_argument(
        "--rewind",
        action="store_true",
        help="allow the cursor to move backwards, re-exposing mail a takeover skipped",
    )
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("bell", help="turn-boundary hook entrypoint; prints hook JSON")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_bell)

    p = sub.add_parser("nudge", help="run the optional nudger: keep the local counter warm, wake idle sessions")
    p.add_argument(
        "--watch",
        action="append",
        default=[],
        metavar="AGENT:PATH",
        help="repeatable; defaults to this directory's registered identity",
    )
    p.add_argument("--poll-interval", type=float, default=30.0)
    p.set_defaults(func=cmd_nudge)

    p = sub.add_parser("hub", help="run the hub in the foreground")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--db", default="~/.local/state/cairn/hub.db")
    p.set_defaults(func=cmd_hub)

    p = sub.add_parser("install-skill", help="install the bundled skill")
    p.set_defaults(func=cmd_install_skill)

    p = sub.add_parser("install-hooks", help="add the turn-boundary bell to the host product's settings")
    p.add_argument("--remove", action="store_true", help="take cairn's hooks back out, leaving any others alone")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("config", help="show or create the config file")
    p.add_argument("--init", action="store_true")
    p.set_defaults(func=cmd_config)

    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Parse and dispatch, converting `CairnError` to an exit code.

    Anything that is not a `CairnError` keeps its traceback. Do not widen this
    catch: a stack trace from a real bug is worth more than a tidy message that
    hides where it came from.

    Parsing is **inside** the try, and that placement is the whole of the fix in
    `_Parser`: with it outside, a malformed command line left by argparse's own
    `error()` exited 2, cairn's "the hub could not be reached". Moving it in
    without `_Parser` would only turn that into a traceback.
    """
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except CairnError as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


def entrypoint() -> None:
    """Console-script entrypoint."""
    sys.exit(run())
