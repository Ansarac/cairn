"""Argument parsing, subcommand dispatch, exit codes.

No business rules live here. Each subcommand resolves configuration, calls one
or two client methods, hands the result to `render`, and picks an exit code.

Exit codes, which a script may rely on:

    0    it worked
    1    it worked, and the answer is "nothing" — empty inbox, no peers
    2    the hub could not be reached
    3    the command cannot be carried out as asked
    4    the hub refused this machine's token
    130  interrupted

`1` and `2` mean opposite things and must never be collapsed. "No mail" is an
answer; "no hub" is a broken pipe. `4` is out of `2` for the next reason along:
a retry fixes an outage and will never fix a credential. `errors.py` has both
arguments in full.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import select
import socket
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from cairn import build, config, notify, nudge, provenance, render, skill
from cairn.client import HubClient
from cairn.errors import CairnError, UsageError
from cairn.wire import (
    BROADCAST,
    MAX_BODY_CHARS,
    Agent,
    Artifact,
    InboxEntry,
    SentEntry,
    WireError,
    normalize_subject,
)

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


def _body(raw: str) -> str:
    """Refuse an oversized body here, where the sender can still do something about it.

    The second of the two admission checks; `store.append` is the one that must
    exist, since an older or foreign client will not run this one. This is the one
    that makes the refusal *usable*, and the difference is the whole reason it is
    duplicated rather than left to the hub.

    Before this cut there was no check on either side. An oversized body was
    stored, answered `200`, and refused only when a reader parsed it — so the
    sender got exit **2** and `hub spoke something unexpected`, which is cairn's
    "the hub is broken or unreachable", under a hub that was neither and a message
    that had already been delivered. `errors.py` argues that 2 and 3 cannot
    collapse because a script's correct response differs: an outage may clear, a
    body that is too long never will. The sender was told to retry the one thing
    that can only fail again, while the thing they would have retracted sat
    unread in the recipient's mailbox poisoning it.

    So the message says what happened to the send, not just what was wrong with
    it. *Nothing was sent* is the fact the old failure got wrong, and it is what
    decides whether the next thing the sender does is retract or rewrite.
    """
    if len(raw) > MAX_BODY_CHARS:
        msg = (
            f"body is {len(raw)} chars and the limit is {MAX_BODY_CHARS}. Nothing was sent. "
            f"Either split it across two messages, or put the bulk behind a reference "
            f"with -a HOST:PATH and say in the body what is at that path"
        )
        raise UsageError(msg)
    return raw


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
        # Both warnings fold what they echo. `{path!r}` was already safe by
        # accident — repr escapes a newline — and `{host}` beside it was not,
        # which is the whole shape of this defect: safety that came from a
        # formatting choice rather than from a rule holds until the next line.
        shown_host, shown_path = render.oneline(host), render.oneline(path)
        if not path.startswith("/"):
            print(
                f"cairn: warning: artifact path {shown_path!r} is not absolute, so it names nothing on {shown_host}",
                file=sys.stderr,
            )
        elif not Path(path).exists():
            print(
                f"cairn: note: {shown_path} is not on this machine — fine if {shown_host} is somewhere else, "
                f"already broken if {shown_host} is here",
                file=sys.stderr,
            )
        artifacts.append(Artifact(host=host, path=path))
    return artifacts


# -- commands -----------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    """Join the network under a name.

    **Registering under a new name in a directory that already holds one is a
    quiet handover, and it now says so.** The pin is per-directory, so a fresh
    session inherits the previous one's identity — its sent log, its read cursor,
    and the only right to withdraw its unread mail, since `retract` refuses
    anybody but the sender. A session that follows the skill's own advice and
    registers on arrival, under the obvious `<name>-2`, walks away from all three
    without being told. One live session came within a command of doing it while a
    broadcast telling two machines to flash a withdrawn board sat unread on the
    hub; what stopped it was the operator's instruction to establish state first,
    not anything cairn said. The way back is to register the old name again in the
    same directory, which is an ordinary returning registration.
    """
    from cairn.adapters import default

    previous = config.current_identity()
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
    print(f"registered as {render.oneline(joined.name)} on {render.oneline(joined.machine)}")
    print(f"  cwd          {render.oneline(joined.cwd)}")
    print(f"  capabilities {render.oneline(', '.join(joined.capabilities)) or '—'}")
    if previous and previous != joined.name:
        print(f"  left behind  {render.oneline(previous)} — this directory was that until now")
        print("    its sends, its read position and the right to `cairn retract` them stay with the name;")
        print(f"    `cairn register {render.oneline(previous)}` here picks it back up")
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
    print(render.oneline(name))
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
    client = HubClient(hub)
    everyone = client.peers(exclude=config.current_identity())
    agents = everyone
    if args.capability:
        wanted = set(args.capability)
        agents = [a for a in everyone if wanted <= set(a.capabilities)]
    # The hub's clock, read off the call that was already made rather than
    # fetched. Every age below it was stamped by that clock, so it is the one
    # this page's arithmetic has to be done on — see `render._ago`.
    now = client.hub_time
    text = render.peers_text(agents, hub, wanted=args.capability, registered=len(everyone), now=now)
    print(render.peers_json(agents, now) if args.json else text, end="")
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
    message = client.send("tell", me, args.recipient, _body(args.body), artifacts=_artifacts(args.artifact))
    print(f"sent seq {message.seq} to {render.oneline(message.recipient)}{_reach(client, message.recipient, me)}")
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
        "ask", me, args.recipient, _body(args.body), correlation_id=correlation, artifacts=_artifacts(args.artifact)
    )
    print(f"asked seq {message.seq} of {render.oneline(message.recipient)}, correlation {render.oneline(correlation)}")
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
        "reply",
        me,
        args.recipient,
        _body(args.body),
        correlation_id=args.correlation,
        artifacts=_artifacts(args.artifact),
    )
    print(f"replied seq {message.seq} to {render.oneline(message.recipient)} for {render.oneline(args.correlation)}")
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    """Read unread messages, and by default mark them read.

    `--wait` does not change what reading means. The ordinary read happens
    first, and only an empty one blocks — so a question already answered by the
    time this runs is answered immediately, which is the first of the
    constraints docs/design.md §12 item 3 records. What arrives is printed and
    acked exactly as it would have been without the flag: there is no partial
    ack, because there is no second code path for one to live in.

    `--since` does change it, in exactly one way: **a windowed read never
    acknowledges.** The ack is `max(seq)` of what was printed, and under a window
    everything between the cursor and the floor was not printed — so acking would
    step the cursor over mail nobody was shown, which is the one failure this
    command has never had. You acknowledge what you were shown, and a read that
    was shown only part of the queue therefore acknowledges nothing. `--no-ack`
    is not required alongside it and saying so is `render.WINDOW_CLAUSE`'s job.
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
    # Refused rather than clamped, on `cmd_notes`' reasoning and one of its own.
    # `LIMIT 0` returns no rows over a full backlog, so the command reported an
    # empty inbox while mail sat on the hub — and with `--wait` it did so for the
    # whole deadline. The truncation line now catches it in the renderer too, but
    # a caller who typed a page of nothing meant something else.
    if args.limit < 1:
        msg = f"--limit needs to be at least 1, got {args.limit}"
        raise UsageError(msg)
    if args.since is not None and args.since < 0:
        msg = f"--since takes a seq to start after, so it cannot be negative; got {args.since}"
        raise UsageError(msg)
    # Refused rather than combined, and this is the one refusal here that is
    # about semantics rather than about a number. `--since S` with `--wait` is
    # the waiter docs/design.md §12 item 3 rules out by name: "watch for anything
    # after my ask", which is the plausible one, the one most likely to be
    # written, and the one that fails on the exchange that taught the rule — a
    # peer answering an *earlier* `tell`, so the answer that settled the question
    # carried a **lower** seq than the question. A wait floored at S blocks
    # through exactly that answer, and it does so while mail sits unread below
    # the floor, which is the `--limit 0` failure with a deadline attached.
    if args.since is not None and args.wait is not None:
        msg = (
            "--since and --wait cannot be combined: a wait floored at a seq is a wait that can block through "
            "an answer carrying a lower one, which is what --wait is written not to do. Window the read, or "
            "wait on the whole inbox"
        )
        raise UsageError(msg)
    me = config.require_identity()
    client = _client(args)
    if args.wait is None:
        page = client.inbox(me, limit=args.limit, since=args.since or 0)
    else:
        from cairn import waiting

        page = waiting.wait_for_mail(client, me, timeout=args.wait, limit=args.limit)
    messages = page.messages
    entries = [InboxEntry(message=m, provenance=provenance.assess(m)) for m in messages]
    # flush before the ack, not after. stdout is block-buffered off a tty and
    # nothing here handles SIGTERM, so a host killing the command in the window
    # between these two lines would move the cursor past mail that never reached
    # the terminal — the one way this command can lose a message outright.
    # `args.since` rather than `page.since`, and this is the one place the two
    # differ on purpose. The wire carries the floor the hub applied, which is `0`
    # both for "no window" and for "a window at zero"; only the command line knows
    # which was typed, and `render._behind_cursor` has the acceptance run that
    # turned that distinction into a wrong answer in somebody's shift summary.
    shape = {"since": args.since, "matching": page.matching, "floor": page.floor}
    text = render.inbox_text(entries, page.unread, _hub(args), **shape)
    print(render.inbox_json(entries, page.unread, **shape) if args.json else text, end="", flush=True)
    if not messages:
        if args.wait is not None:
            print(f"cairn: waited {args.wait:g}s, still nothing.", file=sys.stderr)
        return EXIT_NOTHING
    if not args.no_ack and args.since is None:
        # `max(m.seq for m in messages)`, and **not** `page.head`. The page now
        # carries the true head of the backlog, which makes it look like the
        # tidier thing to acknowledge and would silently discard every message
        # between the end of this page and that head — a truncated read would
        # eat its own remainder, which is the one failure this command has never
        # had. You acknowledge what you were shown. There is a test.
        #
        # `args.since is None` rather than `not page.since`, so that `--since 0`
        # does not ack. That window excludes nothing, so acking it would be
        # harmless and it would also make the footnote printed above — nothing
        # was marked read — a lie on exactly one spelling of the flag. A rule
        # with one silent exception is a rule nobody can rely on.
        client.ack(me, max(m.seq for m in messages))
    return 0


def cmd_retract(args: argparse.Namespace) -> int:
    """Withhold a message the recipient has not read yet.

    **It fails when the message has been read, and the failure is the useful
    half.** Once a cursor is past it the text is in somebody's context and no
    protocol reaches it; a command that reported success anyway would leave the
    sender believing something untrue at exactly the moment that matters. What it
    says instead is who has already read it, which is what turns "I cannot fix
    this" into "I know who to talk to".

    A broadcast is partial by nature — one message, many mailboxes, one cursor
    each — so this reports both halves rather than picking a verdict.

    **Both halves means both lists of names.** Saying only who was too late looks
    like an answer and is not one: the sender's next act is deciding who still has
    to be caught, and a live session recovered that list by subtracting the named
    failures from a `cairn peers` snapshot — which had already gone stale, because
    a peer registered between the send and the retraction. The names are printed
    only when the hub sent as many as it says it spared; an older hub omits
    `withheld_from` entirely, and half a list is worse than a count.
    """
    me = config.require_identity()
    withdrawal = _client(args).retract(args.seq, me)
    print(f"withdrew seq {withdrawal.message.seq} from {withdrawal.withheld} mailbox{_es(withdrawal.withheld)}")
    if len(withdrawal.withheld_from) == withdrawal.withheld:
        spared = ", ".join(render.oneline(name) for name in withdrawal.withheld_from)
        print(f"  withheld from {spared} — they will never be given it")
    if withdrawal.read_by:
        names = ", ".join(render.oneline(name) for name in withdrawal.read_by)
        print(f"  too late for {names} — already read it; a correction has to be a new message")
    return 0


def _es(count: int) -> str:
    """Pluralise "mailbox". Its own helper because `bell_reason` records what a bare `s` cost."""
    return "" if count == 1 else "es"


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete old messages nobody still has unread.

    Manual on purpose and never a timer. A pipe should not keep years of history,
    but the thing that decides when to clear it is a person who knows what the
    shift was, not a clock nobody is watching.

    It cannot take undelivered mail. "The peer was switched off for a week and
    still got its backlog" is the premise of the product, so anything still unread
    by a registered mailbox stays and is **named** out loud — the operator running
    this was told to protect one particular machine's backlog, and "somebody" does
    not answer that. `kept_by` is empty against an older hub, and the count-only
    wording is kept for exactly that case rather than printing an empty list.
    """
    removed, kept, kept_by = _client(args).prune(args.older_than)
    print(f"pruned {removed} message{'' if removed == 1 else 's'} older than {args.older_than} days")
    if kept:
        plural = "message is" if kept == 1 else "messages are"
        whose = f" by {', '.join(render.oneline(name) for name in kept_by)}" if kept_by else " by somebody"
        print(f"  {kept} older {plural} still unread{whose}, so they stayed")
    if not removed:
        return EXIT_NOTHING
    return 0


def cmd_sent(args: argparse.Namespace) -> int:
    """Read back what this session has sent.

    `cairn inbox` shows only what arrived, so a session that restarted has no
    record of what it already told anyone — which is what this is for, and it is
    the friction docs/design.md §12 item 5 was built from.

    **Reading consumes nothing.** There is no cursor on your own sends and no
    ack follows: you have seen them by definition, so there is nothing here for a
    read to use up and a second run returns the same rows.

    Every row is a fact about this session's own actions, and that is the whole
    reason this surface is safe where `cairn pending` was not. It says what was
    sent. It does not say what was delivered, read or answered, and there is a
    footnote saying so on every reading — see the `--limit` note below for the
    other half of not lying by omission.
    """
    # Refused rather than clamped, exactly as `cmd_notes` refuses it and for the
    # same recorded reason: SQLite reads `LIMIT -1` as "no limit", so a negative
    # number silently returns everything, and `LIMIT 0` renders as "nothing sent
    # from here yet" — a session's whole history reported as an empty one, which
    # is precisely the answer a restarted reader would act on without checking.
    if args.limit < 1:
        msg = f"--limit needs to be at least 1, got {args.limit}"
        raise UsageError(msg)
    me = config.require_identity()
    messages, total = _client(args).sent(me, limit=args.limit)
    entries = [SentEntry(message=m, provenance=provenance.assess_sent(m)) for m in messages]
    # Only the text renderer names the hub, on the same split as `cmd_notes`: a
    # model reads the text and may not know what this directory points at, while
    # whatever invoked `--json` chose the hub one call ago.
    text = render.sent_text(entries, total, _hub(args)) if not args.json else render.sent_json(entries, total)
    print(text, end="")
    return 0 if entries else EXIT_NOTHING


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
        body=_body(args.body),
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
    """Say how much is on that pile now.

    Counted from the index, which groups by exact subject, so writing to `rig-a`
    when only `rig-a/chamber` has notes still reads as the first note — which it is.

    **The first note used to be told "new subject — `cairn notes` lists the ones
    that already exist", and that sentence outlived the world it was written
    for.** It was the only guard there was when a subject came into being as a
    side effect of the first note; since `cairn subject`, the pile was opened
    deliberately one command earlier, by a writer that `store.create_subject`
    would have refused had the name already been taken. Three acceptance sessions
    in a row saw it fire immediately after their own `cairn subject` succeeded —
    advice to go and check a decision they had just been made to take on purpose.
    A warning that cannot be acted on is trained past, and the next one is too.

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
    if match is None:
        return ""
    if match.notes <= 1:
        return " · first note on this subject"
    return f" · {match.notes} notes there now"


def cmd_subject(args: argparse.Namespace) -> int:
    """Open a pile deliberately, correct what it says it is, or close one that is finished.

    `--describe` is a separate verb rather than a second meaning for the
    positional argument, and that is the whole of why it is safe: re-running
    `cairn subject <name> "<text>"` still refuses, so a writer who does not know
    the pile exists cannot overwrite a stranger's sentence by doing the obvious
    thing. Reaching the correction takes a flag nobody types by accident, and it
    reports whose words it replaced. `store.describe_subject` has the evidence.

    The one command that exists because of what happens without it. A subject used
    to come into being as a side effect of writing the first note to it, so
    `soak-441`, `eval-441`, `run-441` and `441` were four piles cairn would create
    without comment, and creating one looked exactly like adding to one. An
    acceptance session did precisely that and said so afterwards.

    The description is what does the work, not the ceremony. It is the line the
    next writer reads in the index before deciding whether their pile already
    exists — which is why `store.create_subject` requires one.
    """
    me = config.require_identity()
    client = _client(args)
    name = _subject(args.name)
    if args.describe is not None:
        if args.description:
            msg = "--describe carries the new text; do not also pass a description argument"
            raise UsageError(msg)
        if args.archive or args.reopen:
            msg = "--describe corrects a label; --archive and --reopen open or close the pile. One at a time"
            raise UsageError(msg)
        pile, replaced = _older_hub(
            lambda: client.describe_subject(name, args.describe, me), "cairn subject --describe"
        )
        print(f"described {pile.subject} · {render.oneline(pile.description)}")
        # The old text is printed, and it is folded like anything else off the
        # wire: it was typed by whoever described the pile last, which is the
        # peer-authored path column zero belongs to cairn on. See `render.oneline`.
        print(f"  it used to read: {render.oneline(replaced)}")
        print("  nobody was told; if that correction matters, say so in a note on the pile")
        return 0
    if args.archive or args.reopen:
        if args.description:
            msg = "--archive and --reopen close or open a pile; they take no description"
            raise UsageError(msg)
        pile = _older_hub(lambda: client.archive_subject(name, me, reopen=args.reopen))
        verb = "reopened" if args.reopen else "archived"
        # The verb agrees with the count, on `render.bell_reason`'s recorded
        # lesson: the one sentence a reader sees at a decision point was
        # ungrammatical for four cuts because nobody read it at count 1.
        kept = "1 note stays" if pile.notes == 1 else f"{pile.notes} notes stay"
        print(f"{verb} {pile.subject} · {kept} readable")
        if not args.reopen:
            print(f"  it takes no new notes until `cairn subject {pile.subject} --reopen`")
        return 0
    if not args.description:
        msg = (
            f'a subject needs a description: cairn subject {name} "<one line saying what it is>".\n'
            f"  it is what the next person reads before deciding whether their pile already exists"
        )
        raise UsageError(msg)
    pile = _older_hub(lambda: client.create_subject(name, args.description, me))
    print(f"opened {pile.subject} · {render.oneline(pile.description)}")
    if pile.subject != args.name:
        print(f"  subject folded from {args.name!r}")
    for line in _above(client, pile.subject):
        print(line)
    print(f'  leave the first note: cairn note {pile.subject} "<what you know>"')
    return 0


def _above(client: HubClient, subject: str) -> list[str]:
    """Say what is already filed on the piles this one sits under.

    **A read rolls up, and only downward.** `cairn notes rig-a` covers everything
    in `rig-a/`; `cairn notes rig-a/soak-441` covers nothing above it. So a fact
    about the rig is invisible to everyone reading the run, and the writer who
    knows it has no way to reach a run pile that does not exist yet. An
    acceptance session hit this filing a bench-equipment fault, worked around it
    with three pointer notes, and named what that is worth: *"it covers the piles
    that existed at 07:12 this morning and nothing after. It's a snapshot
    impersonating a rule."*

    This is the cheapest thing that helps and deliberately not the fix that
    session designed. Read-time ancestor inheritance changes what a *reading* is
    and would tip every run read into the rig's whole history; a `--subtree`
    marker needs a new field and a decision from every writer. What this does
    instead is fire once, at creation, which the same session picked out as the
    one moment worth having: *"it lands at the exact moment a human is present
    and deciding."* It is weaker on purpose — it reaches the creator and nobody
    else — and the argument for the larger version is in `docs/design.md`.

    Guarded like `_pile`, and for the same reason: this is a garnish on a write
    that already succeeded, and an older hub without the index route must not
    turn an opened pile into a failed command. Silent on anything that is not a
    nested name, on an ancestor nobody opened, and on one with nothing filed —
    there is no reading to send anybody to.
    """
    parts = subject.split("/")[:-1]
    if not parts:
        return []
    try:
        index = {s.subject: s for s in client.subjects(archived=True)}
    except (CairnError, WireError):
        return []
    lines = []
    for depth in range(len(parts)):
        found = index.get("/".join(parts[: depth + 1]))
        if found is None or found.notes == 0:
            continue
        counted = f"{found.notes} note{'' if found.notes == 1 else 's'}"
        unanswered = f", {found.open_questions} unanswered" if found.open_questions else ""
        closed = ", archived" if found.archived else ""
        lines.append(f"  above it: {found.subject} · {counted}{unanswered}{closed}")
        lines.append(f"    a read of this pile will not include them: cairn notes {found.subject}")
    return lines


def _older_hub(call, missing: str = ""):  # noqa: ANN001, ANN202 - one thunk, one catch; typing it would be longer than it is
    """Turn "this hub has no such route" into a sentence about the hub's age.

    `client._call` maps a 404 to `Unreachable`, which is right for a garnish —
    `_open_questions` and `_pile` swallow it and carry on. It is wrong here,
    because the route *is* the command: "cannot reach hub" on a hub that just
    answered `cairn notes` sends the reader to check the network, which is the
    one place the fault is not.

    `missing` says which route was absent, because the two ages are different
    hubs. One predates the subject table entirely; one has subjects and only
    lacks the correction route, and telling its operator it "creates piles
    implicitly" would send them looking for a problem they do not have.
    """
    from cairn.errors import Unreachable

    try:
        return call()
    except Unreachable as exc:
        if "404" not in str(exc):
            raise
        if missing:
            msg = (
                f"this hub predates `{missing}`, so it cannot do that yet.\n"
                f"  upgrade the hub; everything else about this subject still works"
            )
        else:
            msg = (
                "this hub predates `cairn subject`, so it has no subject table and creates piles implicitly.\n"
                "  upgrade the hub to get deliberate subjects; until then `cairn note <subject>` still works"
            )
        raise UsageError(msg) from exc


def cmd_settle(args: argparse.Namespace) -> int:
    """Answer an open question, whoever asked it.

    No ownership check, deliberately: the whole reason this exists is a session
    that ended and took its questions with it, leaving a peer holding the answer.
    See invariant I3 — cairn declares intent and does not enforce it.

    The subject is not an argument. It comes from the question being settled, so
    an answer cannot be filed away from its question.
    """
    me = config.require_identity()
    note = _client(args).write_note(
        author=me, body=_body(args.body), settles=args.id, artifacts=_artifacts(args.artifact)
    )
    print(f"note {note.id} on {note.subject} settles question {args.id}")
    return 0


def cmd_supersede(args: argparse.Namespace) -> int:
    """Replace what an earlier note says, without taking it away.

    The correction and the thing it corrects both stay on the pile, linked, and a
    reading marks the older one `SUPERSEDED by <id>`. That is the whole difference
    from writing an ordinary contradicting note, which works only if the reader
    happens to read both — the failure `docs/design.md` §12 records from three
    independent sessions.

    No subject argument, on `cairn settle`'s reasoning: it comes from the note
    being replaced, so a correction cannot be filed away from the claim it
    corrects. And no ownership check — whoever finds out that something is wrong
    is frequently not whoever wrote it down. See invariant I3.
    """
    me = config.require_identity()
    note = _client(args).write_note(
        author=me, body=_body(args.body), supersedes=args.id, artifacts=_artifacts(args.artifact)
    )
    print(f"note {note.id} on {note.subject} supersedes {args.id}")
    print(f"  both stay on the pile; `cairn notes {note.subject}` marks {args.id} as superseded")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Take a note's body out, leaving a tombstone that says who and why.

    The one thing in cairn that removes something. The body genuinely goes,
    because the reason to reach for this is sometimes that it should never have
    been written down; the row stays, so anything pointing at the note still
    resolves and the pile can still say something was here.

    The same command for a human and for an agent, deliberately: there is one
    interface and cairn cannot tell which is driving it. What the skill does
    *not* do is tell an agent to go tidying on its own.
    """
    me = config.require_identity()
    note = _client(args).delete_note(args.id, me, args.reason)
    print(f"deleted note {note.id} on {note.subject}")
    print(f"  the body is gone; the tombstone says: {render.oneline(note.body)}")
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
        # Always ask for everything, then drop what was not asked for here. One
        # round trip either way, and it is the only way to say how many piles the
        # listing is leaving out — an index that quietly omits finished work reads
        # as the map of what exists, and a live session drew exactly that wrong
        # conclusion from it.
        everything = client.subjects(archived=True)
        summaries = everything if args.archived else [s for s in everything if not s.archived]
        hidden = len(everything) - len(summaries)
        clock = client.hub_time
        text = render.subjects_text(summaries, _hub(args), clock, hidden)
        print(render.subjects_json(summaries, clock, hidden) if args.json else text, end="")
        return 0 if summaries else EXIT_NOTHING
    entries, total, removed = client.notes(
        subject, open_only=args.open, find=args.find, limit=args.limit, deleted=args.deleted
    )
    read = [e.checked(provenance.assess_note(e.note)) for e in entries]
    # The hub's clock, off the call just made. `notes` is the surface that asks the
    # reader to judge how stale something is and then, until this, printed no
    # instant to judge it against — see `render.STALENESS_TAIL`.
    scope = {
        "subject": subject,
        "open_only": args.open,
        "find": args.find,
        "now": client.hub_time,
        "removed": removed,
        # Which page this is, not just which filter produced it. `removed` counts
        # tombstones in scope, so it equals `total` in the tombstone view, and a
        # renderer that could not tell the two apart printed the count as a
        # deletion tally over a page that *was* the deletions.
        "deleted": args.deleted,
    }
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
        print(f"forgot where {render.oneline(args.name)} was; the next send will learn it again")
        return 0
    print(f"no pin recorded for {render.oneline(args.name)} in this directory")
    return EXIT_NOTHING


HOOK_INPUT_WAIT_SECONDS = 0.2
"""How long the bell will wait for the host to say which event this is.

Short on purpose, and a wait rather than a read: this runs at every turn
boundary, and a `read()` on a stdin nobody closes does not fail, it hangs — with
the turn behind it. A missed event name costs the wrong envelope on one bell; a
hung hook costs the session.
"""

HOOK_INPUT_MAX_BYTES = 65536
"""One read, capped. The event name is in the first few dozen bytes."""


def _hook_input() -> str:
    """Return what the host wrote to our stdin, or empty if it wrote nothing.

    Every failure mode answers "nothing", including the interesting one: run by
    hand at a terminal there is no host and no event, and waiting on a keyboard
    would be a hang with a friendly face.
    """
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            return ""
        ready, _, _ = select.select([fd], [], [], HOOK_INPUT_WAIT_SECONDS)
        if not ready:
            return ""
        return os.read(fd, HOOK_INPUT_MAX_BYTES).decode("utf-8", "replace")
    except (OSError, ValueError, AttributeError):
        return ""


BELL_TEST_COUNT = 1
"""The count `cairn bell --test` pretends to have.

One rather than a rounder number because one is the count whose sentence was
ungrammatical from the first commit through three cuts — `1 unread message …
read them`. If an operator is going to see this line once before trusting it,
it may as well be the reading that has been wrong before."""


def _bell_test() -> int:
    """Run the configured bell command in the foreground and say what happened.

    The operator's half of this feature, and the reason `notify.fire` is allowed
    to be silent. Three exit codes, so a script can tell the three states apart:
    `0` a notification would go out, `1` nothing is configured, `3` something is
    configured and does not work — malformed, missing, stuck, or exiting
    non-zero. `1` rather than `3` for the absent case keeps the house meaning of
    both: an unset optional key is "asked, nothing to report", not a refusal.
    """
    command = config.bell_command()
    if not command:
        print(f"cairn: no bell_command in {config.config_path()}; nothing runs when the bell rings")
        return EXIT_NOTHING
    me = config.current_identity() or "nobody"
    reason = render.bell_reason(BELL_TEST_COUNT)
    argv = notify.build_argv(command, BELL_TEST_COUNT, me, reason)
    result = notify.probe(argv, notify.bell_env(BELL_TEST_COUNT, me, reason))
    print(render.bell_test_report(result, BELL_TEST_COUNT))
    return 0 if result.ok else 3


def _fire_bell_command(count: int, me: str, reason: str) -> None:
    """Hand the same ring to a person, if the operator asked for that. Never raises.

    Placed after `latch_belled` and before the payload is printed, which pins two
    properties. It rings on the same latch as the agent's bell — once per new
    head, both audiences at once, never one without the other. And it cannot stop
    the payload reaching stdout: `notify.fire` swallows its own failures, so there
    is no path where a broken notification command costs the session its bell.

    A `UsageError` from a malformed `bell_command` is left to `cmd_bell`'s own
    catch, which prints `{}` and exits 0 — a hook may not fail loudly, even about
    its own configuration. `cairn bell --test` is where that is said out loud.
    """
    command = config.bell_command()
    if command:
        notify.fire(notify.build_argv(command, count, me, reason), notify.bell_env(count, me, reason))


def cmd_bell(args: argparse.Namespace) -> int:
    """Emit a bell for whichever agent product invoked us, in that event's shape.

    Four properties matter more than anything this function does.

    **It never carries content.** The bell says how many messages are waiting
    and how to read them. Peer text in a hook is unattributable and gets treated
    as an injection attempt, correctly.

    **It never rings twice for the same mail.** If the reader chose not to open
    the inbox, that is the reader's call; ringing again every turn would be a
    loop, not a reminder.

    **It never fails loudly.** A hook that errors degrades the session it is
    attached to, so an unreachable hub means an empty response and exit 0.

    That property is kept and it had a hole underneath it: *never loudly* had
    become *never at all*. `WireError` is a `ValueError`, so a page this build
    could not parse was caught here and reported as an empty mailbox — the
    recipient of a poisoned inbox was told there was no mail, at every turn
    boundary, forever. Measured with an oversized body, which `store.append` now
    refuses; the swallow is general, so closing that one entrance does not close
    this. `{}` and exit 0 are still what stdout gets, because that is the hook
    contract and a session must not be degraded by its own bell. The difference
    is that the reason now goes to **stderr**, which reaches a hook log and a
    human and never reaches the model's context — the one channel here that can
    carry a fact without carrying a consequence.

    **It speaks the shape the invoking event expects**, which the adapter owns
    because the shape is a fact about one product. The latch below advances on
    the ring, not on the reading, and that is only safe while every event cairn
    installs on actually delivers — for three cuts one of them did not, so the
    ring that reached nobody also silenced the one that would have. The
    adapter's `bell_payload` carries the measurement.

    **The same ring may also reach a person**, when the operator has configured
    `bell_command`. That is one call, `_fire_bell_command`, and it changes none
    of the four properties above: same latch, same count, same absence of
    content. `notify.py` argues why routing a bell through a channel cairn does
    not trust is safe, and names the thing nobody may build on top of it.

    It reads a local counter when a nudger is maintaining one, and asks the hub
    only when nobody is. This runs at every single turn boundary, so the common
    case has to cost a `stat` and a small read rather than a round trip — but
    "the counter says zero" is only worth believing while something is still
    writing it, which is what the freshness check is for.
    """
    from cairn.adapters import default

    if args.test:
        return _bell_test()
    try:
        me = config.current_identity()
        if not me:
            print("{}")
            return 0
        if nudge.counter_is_fresh(me):
            count, head = nudge.read_unread(me)
        else:
            # Both off the page's totals, not off the page. Derived from the
            # capped window, `head` stopped advancing as soon as the backlog
            # passed `--limit`; the latch below then pinned to it and this bell
            # was silent for good. The count was wrong in the same breath, so a
            # reader with 200 waiting was told about 50 — right up until it was
            # told about none. See `wire.InboxPage`.
            page = _client(args).inbox(me, limit=args.limit)
            count, head = page.unread, page.head
        if not count or head <= nudge.read_belled(me):
            print("{}")
            return 0
        reason = render.bell_reason(count)
        payload = default().bell_payload(_hook_input(), reason)
        nudge.latch_belled(me, head)
        _fire_bell_command(count, me, reason)
        # ensure_ascii=False so the reason reads as itself in the hook log rather
        # than as \uXXXX escapes; hook stdout is UTF-8 and render owns the wording.
        print(json.dumps(payload, ensure_ascii=False))
    except (CairnError, OSError, ValueError) as exc:
        print("{}")
        # Folded, because a `WireError` from `normalize_subject` quotes the value
        # it refused and that value came off the wire (I1, column zero) — stderr
        # is a smaller door than stdout, not a different rule.
        print(
            f"cairn: bell found nothing it could read, so it rang for nothing: {render.oneline(str(exc))}",
            file=sys.stderr,
        )
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
    # Resolved here rather than inside `serve`, so `hub.py` goes on knowing
    # nothing about where this machine keeps its configuration.
    serve(SqliteStore(db), host=args.host, port=args.port, token=config.token())
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    """Install the bundled skill, and say which of the three cases it was.

    Exit code stays 0 for all three: none of them is a failure, and `unchanged`
    especially is the answer somebody re-running this wants to be told rather
    than warned about. `skill.install_skill` argues why the case is worth a line,
    and `render.skill_installation` why the case comes before the path.
    """
    from cairn.adapters import default

    print(render.skill_installation(skill.install_skill(default().skills_dir())))
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
    """Show or create the config file.

    **The token line says whether and from where, never what.** This page is the
    thing somebody pastes into a message when asking why their machine cannot
    reach the hub, so printing the secret would make the one useful diagnostic
    unshareable. The source is the half that matters anyway: "there is a token"
    was rarely the question, and "the environment is overriding the file I just
    edited" always is.

    **The same line means two things and no wording fixes that**, so none is
    attempted. `config.token` is read by both ends of the wire, so on an agent
    machine this reports what cairn will *send* and on a hub what it will
    *require* — and cairn cannot tell which kind of machine it is on, because
    that is decided by whether somebody runs `cairn hub` here. Stating the value
    and leaving the consequence to the reader is the honest form.
    """
    if args.init:
        # `--init` is the way out of a config this build refuses to read, so it
        # must not be the one command that needs to read it. It only consults the
        # old file to carry the hub across; when that fails, say what is being
        # lost rather than overwriting in silence — the unreadable bytes may still
        # be the only copy of a token.
        try:
            hub = config.hub_url(args.hub)
        except UsageError as exc:
            print(f"cairn: {exc}", file=sys.stderr)
            print("cairn: replacing it; anything it held, including a token, is gone", file=sys.stderr)
            hub = args.hub or config.DEFAULT_HUB
        print(f"wrote {config.write_default_config(hub)}")
        return 0
    source = config.token_source()
    print(f"hub          {config.hub_url(args.hub)}")
    print(f"config file  {config.config_path()}")
    print(f"token        {f'set ({source})' if source else 'not set'}")
    print(f"state dir    {config.state_dir()}")
    print(f"identity     {render.oneline(config.current_identity() or '') or '—'}")
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


class _Version(argparse.Action):
    """Print the version line, building it only when `--version` is actually passed.

    `action="version"` takes a finished string, so the obvious form —
    `version=build.describe()` — would run at `add_argument` time and put an
    `importlib.metadata` scan of `sys.path` on **every** invocation of every
    command. `cairn bell` runs at every single turn boundary and its docstring
    commits to costing a `stat` and a small read rather than anything larger, so
    the obvious form is the one that quietly regresses the hot path. Five lines
    here buy that back; do not tidy them away.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: object,
        values: object,
        option: str = "",
    ) -> NoReturn:
        """Print and exit 0, as argparse's own version action does."""
        _, _, _ = namespace, values, option
        print(build.describe())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - one flat statement per flag; splitting it hides the surface
    """Build the argument parser."""
    parser = _Parser(prog="cairn", description="Cross-machine messaging for coding agent sessions.")
    parser.add_argument("--version", action=_Version, nargs=0, help="show the version and which build this is")
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

    p = sub.add_parser(
        "inbox",
        help="read unread messages",
        description=(
            "Read what is waiting for you, oldest first, and by default mark it read. "
            "`--since SEQ` walks a backlog a page at a time without consuming it: pass the last seq "
            "you were shown and the next read starts after it. A windowed read never marks anything "
            "read, because it was never shown the part of the queue below the window."
        ),
    )
    p.add_argument("--limit", type=int, default=50)
    p.add_argument(
        "--since",
        type=int,
        default=None,
        metavar="SEQ",
        help="show only mail after this seq; marks nothing read, and cannot be combined with --wait",
    )
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
        "sent",
        help="read back what this session has sent",
        description=(
            "Read back what you have sent. `cairn inbox` shows only what arrived, so a session "
            "that restarted has no record of what it already told anyone. Reading consumes "
            "nothing — there is no cursor on your own sends. It says what you sent; it does not "
            "say what was delivered, read or answered."
        ),
    )
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sent)

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

    p = sub.add_parser(
        "subject",
        help="open a pile deliberately, or close one that is finished",
        description=(
            "Open a subject before writing notes to it. A subject is a rig, a run or a board, and it is "
            "opened deliberately so that four spellings of one thing do not become four piles nobody can "
            "find. The description is the line the next person reads in `cairn notes` before deciding "
            "whether the pile they were about to create already exists."
        ),
    )
    p.add_argument("name", help="the thing this is about, e.g. rig-a or eval-441")
    p.add_argument("description", nargs="?", help="one line saying what it is")
    p.add_argument("--archive", action="store_true", help="close it to new notes; reading is unaffected")
    p.add_argument("--reopen", action="store_true", help="undo an archive")
    p.add_argument(
        "--describe",
        metavar="TEXT",
        help="correct the description of a pile that already exists; prints what it replaced",
    )
    p.set_defaults(func=cmd_subject)

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
    p.add_argument("--archived", action="store_true", help="include piles that have been closed")
    p.add_argument("--deleted", action="store_true", help="list the tombstones instead of the notes")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser(
        "supersede",
        help="replace what an earlier note says, keeping both",
        description=(
            "Say that a note has been overtaken. Both stay on the pile and a reading marks the older one "
            "SUPERSEDED, so a reader who finds one finds the other. The subject comes from the note being "
            "replaced, so a correction cannot end up filed away from the claim it corrects."
        ),
    )
    p.add_argument("id", type=int, help="the note id printed by `cairn notes`")
    p.add_argument("body", help="what is true now")
    p.add_argument("-a", "--artifact", action="append", default=[], metavar="HOST:PATH")
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser(
        "delete",
        help="take a note's body out, leaving a tombstone",
        description=(
            "Remove what a note said. The body genuinely goes — this is the command for something that "
            "should not have been written down — while the row stays, so anything pointing at it still "
            "resolves and the pile still says something was here, who took it out and why. To correct a "
            "note rather than remove it, use `cairn supersede`."
        ),
    )
    p.add_argument("id", type=int, help="the note id printed by `cairn notes`")
    p.add_argument("reason", help="why it went; it replaces the body, so do not repeat what it said")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("forget", help="drop this directory's pin for a name that has legitimately moved")
    p.add_argument("name")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser(
        "retract",
        help="withhold a message the recipient has not read yet",
        description=(
            "Pull back one of your own messages. It works only while the message is still in the pipe: "
            "once a recipient's cursor is past it the words are in somebody's context and cairn cannot "
            "reach them, so this refuses and tells you who read it. A broadcast is partial — it reports "
            "how many mailboxes it spared and who it was too late for."
        ),
    )
    p.add_argument("seq", type=int, help="the seq printed by `cairn tell` or `cairn sent`")
    p.set_defaults(func=cmd_retract)

    p = sub.add_parser(
        "prune",
        help="delete old messages nobody still has unread",
        description=(
            "Clear old traffic off the hub. Messages are a pipe rather than sediment, so this deletes "
            "outright — notes are the thing meant to outlive a session, and they are never touched. "
            "Anything still unread by a registered mailbox stays put and is counted, because a peer that "
            "was switched off for a week is supposed to come back to its backlog."
        ),
    )
    p.add_argument("--older-than", type=int, default=30, metavar="DAYS", help="cutoff, on the hub's clock")
    p.set_defaults(func=cmd_prune)

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
    p.add_argument(
        "--test",
        action="store_true",
        help="run the configured bell_command now and report it, instead of ringing",
    )
    p.set_defaults(func=cmd_bell)

    # `nudge` is **withdrawn**: no subparser, so there is no way to start the
    # daemon. `nudge.py`, `terminal.py` and the adapter's session lookup are all
    # still here and still tested, because the measurements that killed this are
    # worth more than the keystrokes to delete it and a future cut may want the
    # SSE half back. `docs/design.md` §5 has the reasoning and the date.
    #
    # To unseal: restore this subparser. Nothing else was removed. Read §5 first
    # — two defects found on the day it was withdrawn are still in the code,
    # `cli._watches` resolving a pid exactly once and `_usability` ranking
    # candidates without asking whether they have a pane.

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
