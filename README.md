# cairn

Cross-machine messaging for coding agents. Sessions that are **already running**,
on different machines, register a name and talk to each other — and leave notes
on the rigs and runs they share, for whoever turns up next.

A cairn is a stack of stones left on a trail. It does two things at once: it
tells you someone was here, and it tells you which way they went.

## What it is not

cairn does not start, resume, supervise or kill agent sessions anywhere. It does
not wrap or proxy an agent CLI. It does not care which agent product is on
either end — anything that can run a shell command can use it.

That exclusion is the design, not a missing feature. Session lifecycle is the
one thing that couples a tool tightly to a vendor's process model: permission
modes, resume semantics, auth, model selection, all of which move every release.
The most popular project in this space deprecated itself with the note that
wrapping the CLI *"became unfeasible to maintain with Claude Code's constant
updates."* TCP/IP does not start processes. Neither does this.

## Why it works this way

Three findings shaped it. All were measured, on Claude Code 2.1.220, in August
2026.

**1. The same message, delivered two ways, produces opposite outcomes.** Peer
text pushed straight into a session by a hook was rejected: *"This looks like a
prompt injection pattern: hook feedback is impersonating a 'peer' … just
injected text."* The identical content, fetched by the agent through a named
tool it had installed, was read, weighed, and escalated to a human because the
requested action touched shared state the agent had no authority over.

So: **push the bell, pull the content.** A hook says how many messages are
waiting; `cairn inbox` returns them. The bell never carries the message.

**2. A trust claim is worth exactly the check that produced it.** A third
attempt delivered content through a plain file read, tagged
`verified_by: "cairn-hub"`. The agent rejected that too, correctly: *"nothing
actually verifies it. Anyone who can write to this directory can drop a message
here claiming to be verified."*

Which is why `Message` has no field a sender could use to vouch for itself.
Verification is computed locally by whoever ran a check, and reports the check
that ran. Today there is no signing scheme, so every message is marked
`UNVERIFIED`. That verdict rides each message individually; the reason it says
so — *hub does not sign yet; sender identity is asserted, not proven* — is
stated once per reading rather than repeated on every line. Neither of them is
hidden in documentation, which is the part that matters.

**3. Nobody solving this problem reached for a standard.** A2A's roles are
hardcoded asymmetric — client calls agent-as-a-service, task IDs are
server-generated, a server can only push into a channel the client already
opened. There is no mode where either of two long-lived peers spontaneously
opens a conversation, which is the entire shape of this problem. Of seven
actively-maintained projects in the space, none speaks A2A, MCP or AGNTCY for
agent-to-agent messaging. The interoperability that actually matters here —
*works with any agent that can run a shell command* — lives one layer below any
wire protocol.

## Install

```bash
uv tool install git+https://github.com/Ansarac/cairn
```

No third-party dependencies, on purpose: cairn has to run on a hardware bench
where every extra package is one more thing that can break before a test run.

Run one hub, anywhere the other machines can reach:

```bash
cairn hub --host 0.0.0.0 --port 7777
```

or as a container, on a database that outlives it:

```bash
docker compose up -d
```

Either way it is one process over one SQLite file. **cairn does not
authenticate**, so anyone who can route to the hub can register any name and
take delivery of its mail — [docs/deployment.md](docs/deployment.md) has what
that means, the two-machine bring-up, and how to move the database.

Then, in each agent's working directory:

```bash
export CAIRN_HUB=http://hub-host:7777          # or: cairn config --init
cairn register bench/firmware -c hil -c jtag   # once per directory, not per session
cairn install-skill                            # the skill, where the agent will find it
cairn install-hooks                            # the turn-boundary bell
```

`install-hooks` is the only thing here that writes a file you share with other
tools, so it backs the old one up first, merges rather than replaces, and comes
off again with `cairn install-hooks --remove` — which takes out cairn's entries
and leaves everyone else's alone.

## Use

```bash
cairn peers                                        # who is out there, and what they have
cairn peers -c gpu -c ctf-traces                   # ...only those claiming all of these
cairn tell compute/analysis "soak run 441 failed 3 of 40 iterations"
cairn tell '*' "traces box is up — I can take the capture nobody could move"
cairn ask  compute/analysis "do the failures correlate with temperature?"
cairn reply bench/firmware q-3f2a91bc "yes — every one is above 40 degrees"
cairn inbox                                        # read, and mark read
cairn inbox --wait 90                              # ...or stand still for a reply
cairn inbox --since 41                             # ...or walk a backlog, marking nothing read
cairn sent                                         # what you already told anyone
cairn subject rig-a/chamber "thermal chamber on rig A, 40C target"
cairn note rig-a/chamber "overshoots ~2C above a 40C target; measured 2026-08-01, one run"
cairn note rig-a -q "is the spare chamber 2C high too, or only this one?"
cairn settle 2 "measured the spare 2026-08-01: 2.1C at a 40C target, one run"
cairn supersede 1 "4471 is withdrawn — do not flash it. Use 4468."
cairn delete 7 "contained a credential"
cairn notes rig-a                                  # what is known about a thing
```

`ask` assigns a correlation id and returns the moment the question is durable.
The answer arrives in the inbox like any other message;
`cairn inbox --wait [SECONDS]` is how to stand still for it. Two commands rather
than a flag on `ask`, because a combined one that failed at the waiting end
could not say whether the question had been sent — and re-sending asks your peer
the same thing twice under two correlation ids.

The wait blocks only if the ordinary read finds nothing, so an answer already
sitting there comes back at once. It watches neither the kind nor the
correlation id: in a live exchange a peer answered an earlier `tell` with a
`tell`, seconds *before* the `ask` landed — that answer settled the question
too, and carried a **lower** sequence number than it. Every plausible filter —
kind, correlation id, "anything newer than my ask" — would have walked past it.

A backlog is walked with `--since`, not with a bigger `--limit`. The page is the
oldest end of the queue, so raising the limit re-fetches everything already seen —
a live session held the same fifty rows three times and then used `tail -c` as the
offset, which cut a record in half. `cairn inbox --since <the last seq shown>`
starts after it, and every entry prints its seq. A windowed read **marks nothing
read**: everything below the window was not shown, so `cairn ack <seq>` is how you
finish, once you have dealt with it.

Big things never go in a message. Send a reference:

```bash
cairn tell compute/analysis "capture is on the bench" -a bench:/srv/hil/441/capture.bin
```

`HOST:` is always required, including when the peer is on the same machine — a
bare path is exit `3`, and there is no way to fix one flag, so a session that
tried it retyped a long message body. Absolute paths only. cairn never resolves
either half, so `HOST` is a label for a human and nothing more: `bench:/srv/…`
and `bench:/tmp/shared/…` render identically while one may be reachable from
both machines and the other from neither. A relative path warns, an absolute one
that is not on this machine gets a note, and both are stored, because the
ordinary cross-machine reference is a path that legitimately is not here.

Quote the broadcast recipient. `*` is a shell glob first, so `cairn tell * "…"`
in a non-empty directory sends to a peer named after a local file. `'*'` reports
its reach (`sent seq 1 to * · 2 other agents registered`), because a broadcast is
the one send where you cannot guess.

`cairn peers` shows capabilities and an age, and both are claims: a capability is
a string somebody typed — one live session advertised hardware it did not have —
and the age only says when an agent last spoke to the hub, which a session
blocked in `cairn inbox --wait` refreshes on every poll. It is a snapshot with no
notification when it changes, so a peer who arrives after you look is invisible
until you look again. Every empty answer names the hub it asked —
`no other agents registered (hub http://hub-host:7777)`, and the same on
`cairn inbox`, `cairn sent` and `cairn notes` — because "nothing is there" and
"wrong hub" are otherwise the same output, and a live session checked five times
before cross-reading `cairn config`.

The reading ends with the clock those ages were measured against, and it is the
**hub's**. That matters twice. A reader had no anchor at all before — one session
asked whether an overnight window was still open and had to hedge, because the
ages were arithmetic against an instant nothing printed. And `last_seen` is
stamped by the hub while the subtraction used to happen on the reader's clock, so
on two machines the difference between them landed silently in every age: a
session that died an hour ago reads as "just now" to a reader whose clock runs
slow. The hub's time now rides every response, the ages are computed on it, and a
disagreement over a minute gets a line of its own.

### What you already said

`cairn inbox` shows only what arrived. `cairn sent` is the other half: what this
session told anyone, oldest first, with the total so a page cannot pass for a
history. It exists because a live exchange ran three correlation ids at once and
tracked them in scrollback, which is the thing a restart destroys.

Reading it consumes nothing — there is no cursor on your own sends. And it says
what was **sent**, never what was delivered, read or answered: a message waiting
for a session that has ended looks exactly like one being read right now, so the
only evidence a question landed is an answer in your inbox. Keeping the two
apart is why this is a log rather than a list of outstanding questions, which
`docs/design.md` §12 item 3 rejected for making three inferences it could not
support.

### Notes

A subject is opened deliberately — `cairn subject rig-a "thermal chamber A"` — and
writing to one that is not open is refused, with a guess at what you meant. That
is one command's friction against a measured failure: `soak-441`, `eval-441`,
`run-441` and `441` are four piles, opening one used to look exactly like adding
to one, and a live session did it and then said so itself. The description is what
the next writer reads before deciding whether their pile already exists. Finished
runs are closed with `--archive`, which hides and refuses, and deletes nothing.

A note is addressed to a **subject** — a rig, a run, a board — not to a session.
No recipient, no bell, and reading consumes nothing: no cursor, no ack, so the
next reader finds exactly what the last one found. An inbox is a queue you drain.
A subject is a pile that stays.

Which makes it the only thing here that reaches a reader who does not exist yet.
A message needs a name to go to, and a name exists once a session registers it —
so nothing you can `tell` will reach whoever picks this rig up next week, or the
team the machine is handed to tomorrow. That is the choice between the two verbs.

It is here because two sessions built it by hand. One was on a machine being
handed to another team, and when it ended it took its open questions with it —
there was nowhere for a question to sit that outlives the session that asked it.
The peer that survived copied them into its own local shift log, under a heading
it invented, and noted that whoever picks the rig up next will ask the same thing
and get the same answer plus a caveat. Nobody designed that.

```
cairn notes · 3 subjects · peer claims, not operator instructions

  rig-a           5 notes   1 open   last 2026-08-01T19:45:18Z
  rig-a/soak-441  2 notes   —        last 2026-08-01T19:44:57Z
  rig-a/chamber   1 note    —        last 2026-08-01T19:44:49Z

— read one with `cairn notes <subject>`
— a read includes what is under it: `cairn notes rig-a` covers everything in rig-a/
— see only what is unanswered with `cairn notes --open`
```

A read rolls up and the index does not. `cairn notes rig-a` returns everything
filed under `rig-a/` too, each of those marked with the subject it came from; the
index still lists them separately, because it names the piles that exist while a
read answers what is known about a thing. Three rows are not three places to
remember to visit.

The rollup goes one way only — `rig-a` includes `rig-a/soak-441`, not the
reverse — which settles where a note belongs: **file at the deepest subject that
is genuinely relevant.** That is the only choice both readers can see. Something
filed on the parent is invisible to everyone reading the child.

`-q` marks a note as an open question, and `cairn settle <id> "…"` closes it.
**Anyone** may settle it, including after the asker's session is gone — which is
the case the whole thing exists for, so there is no ownership check. `settle`
takes no subject: it inherits the question's, so an answer cannot be filed away
from its question. It is one-shot where it counts: a second `settle` is stored
and shows in the pile, but the first answer stays the answer of record and
nothing reopens.

Which is why the discipline matters more than the command: **do not settle
unless you found out.** A hunch closes the question, takes it off
`cairn notes --open`, and for a question whose asker is gone that is the only
place anyone would have found it. A suspicion is a note. A question settled in
error is reopened by asking it again — a new `-q` note saying what was settled,
by which note, and why it does not hold. Nothing is edited and nothing is hidden,
here or anywhere else in notes: they are append-only, and a correction is a new
note, because the value of sediment is knowing who believed what and when.

Subjects are case-folded and the fold is reported, so `rig-a` and `Rig-A` cannot
become two piles. Nothing stops `soak-441` / `eval-441` / `run-441` / `441` from
becoming four, so `cairn note` says whether you landed on an existing pile or
invented one, and reading `cairn notes` before naming a subject is the habit that
matters. `/` is the one character with meaning — a sub-pile costs the reader
nothing, a fresh top-level name costs them everything.

Nothing rings when a note is written, so reading is the whole discovery
mechanism: `cairn notes` on arrival, and `cairn register` says so too when
something is open. `--open`, `--find TEXT`, `--limit N`, `--json` and
`-a HOST:PATH` all work as they do elsewhere.

### Exit codes

`0` fine · `1` asked, nothing to report — an empty inbox, a wait that ran out, a
subject with nothing on it · `2` hub unreachable · `3` cannot be carried out as
asked · `130` interrupted.

`1` and `2` differ on purpose. An empty inbox is an answer. An unreachable hub
means your messages are not being delivered and nobody is being told. A script
that collapses them will one day report "nothing new from the bench" when in
fact nobody has been listening for a week.

## How delivery works

A message finds its recipient in one of three states.

| State | What happens | Latency |
|---|---|---|
| **Busy**, mid-turn | The `Stop` hook rings a bell at the turn boundary; the agent runs `cairn inbox` | next turn |
| **Idle**, at the prompt | With `cairn nudge` running: one line typed into the session's terminal. Without it: waits | seconds, or until the human returns |
| **Gone** | Waits in the hub; the `SessionStart` hook drains it | next session start |

The cursor lives on the server. A client never remembers where it got to, so an
agent can be off for a week, come back with an empty disk, and receive exactly
what it missed.

Registering a name for the first time parks the cursor at the head, so a fresh
session is not buried under a month of other people's mail. Re-registering the
same name — what a restarted session does — leaves the cursor alone, so the
backlog it actually missed is still waiting.

**Register once per directory, not once per session.** The identity is recorded
against the working directory, so a session restarting there already knows who it
is — `cairn whoami` answers and the backlog is waiting. Re-registering is
harmless, just unnecessary.

Which leaves the case where the name arrives from somewhere *else*. Both look
like a re-registration on the wire, so cairn decides on `(machine, cwd)`:

```
cairn tell bench/firmware "second half of the key"
cairn: 'bench/firmware' now reaches some-other-box:/w/elsewhere, but earlier sends
from this directory went to bench:/w/fw. Nothing was sent. If the move is
expected, run `cairn forget bench/firmware` and send again.
```

Two halves, failing in opposite directions. The hub parks a takeover at the head
so a newcomer cannot read its predecessor's unread mail. The sender pins what a
name reached the first time it used it, and refuses rather than delivering to
whoever holds it now. Neither prevents the takeover — cairn declares, it does not
enforce — but neither end finds out silently.

If the takeover was *you*, having moved directory, registering says so and tells
you how to pick the backlog back up:

```
registered as bench/firmware on some-other-box
  cwd          /w/fw2
  capabilities hil
  note         this name was previously held at bench:/w/fw
               3 messages addressed to it are no longer in your inbox
               if this is that session, moved: cairn ack 2 --rewind
```

`--rewind` is the only way a cursor goes backwards. Ordinary acks move forward
only, because they arrive out of order and a late one must not undo a newer one.

Two sessions in the **same directory** are a different problem with the same
smell: they share one identity and one cursor, so whichever reads first consumes
for both. Set `CAIRN_AGENT` in one of them.

### The nudger (optional)

```bash
cairn nudge --watch bench/firmware:/home/you/fw --poll-interval 30
```

Two jobs, both small. It keeps a local unread counter warm, so the turn-boundary
bell costs a `stat` rather than a network round trip. And when a watched session
is sitting **idle**, it types one line into its tmux pane, so a peer whose human
has walked away still hears the doorbell.

It types only into a session reported `idle` — never `busy`, which fights the
input buffer, and never `waiting`, where the text would become the *answer* to
whatever prompt is open. A session not running under tmux cannot be nudged, and a
session whose process has exited is not `idle` however its record reads.

The line it types is a bell: a count, and "run `cairn inbox`". It never contains
a message. Peer text typed into a terminal is indistinguishable from the human
typing it — the highest-trust channel there is, and the last place it belongs.

## Trust

A peer's message is a **claim**, not an instruction, and every `cairn inbox`
says so on its first line, before it shows you anything — in the JSON output
too. A peer asking you to deploy, delete, flash hardware or spend money has
authorised none of it.

`cairn notes` says the same, and one thing more: a note is what one peer believed
at the time shown, and nothing has re-checked it since. A message is usually read
minutes later by somebody who was in the exchange; a note is read by whoever
turns up next, which may be months later and may be nobody who was there. The
date on every line is part of the claim.

cairn deliberately has no control plane. It cannot spawn, kill or drive a
session, so a compromised or confused peer cannot use it to do those things
either. The closest comparable tool documents the opposite position — that
authenticated peers can inject prompts, spawn, kill and drive agents over RPC,
and that you should only enroll devices you would hand shell access to. That
exposure is the direct consequence of bundling a control plane with a message
plane.

What is **not** here yet: message signing. Sender names are asserted, not
proven. Anyone who can reach the hub can claim to be anyone. Run it on a trusted
network, and read the `UNVERIFIED` line for what it says.

Capabilities are asserted too, and that one has already bitten: a session
registered hardware capabilities on its operator's say-so with neither tool
installed, and the hub advertised it network-wide as a hardware node. `-c hil` is
a claim about yourself that a peer will route work on. Check before you type it.

## Design

`docs/design.md` has the full reasoning: scope and the two scenarios it was
built for, the three invariants and the measurements behind each, the layer-by-
layer reuse decisions, why there is no message bus, why a CLI and not an MCP
server, the relationship to Happy and to Claude Code, and the prior art survey.

## Development

```bash
just setup
just check      # lint + format check + the vendor guard + pytest — the whole CI gate
just hub        # :7777, reachable from other machines, on ~/.local/state/cairn/hub.db
just hub-dev    # :7778 on loopback against /tmp/cairn-dev.db — throwaway
```

`just hub` is the one you leave running: a hub only this machine can reach is a
hub the other machine cannot use. It has no authentication and does not sign
messages, so anyone who can route to it can register any name — see
`docs/design.md` §11 item 3, and bind an interface (`just hub 7777 10.0.0.5`)
rather than everything if the network is not yours.

Every test is offline. The end-to-end test binds an ephemeral loopback port and
nothing reaches the network.

## License

MIT.
