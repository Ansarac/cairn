# cairn

Cross-machine messaging for coding agents. Sessions that are **already running**,
on different machines, register a name and talk to each other.

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
cairn tell compute/analysis "soak run 441 failed 3 of 40 iterations"
cairn ask  compute/analysis "do the failures correlate with temperature?"
cairn reply bench/firmware q-3f2a91bc "yes — every one is above 40 degrees"
cairn inbox                                        # read, and mark read
cairn inbox --wait 90                              # ...or stand still for a reply
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

Big things never go in a message. Send a reference:

```bash
cairn tell compute/analysis "capture is on the bench" -a bench:/srv/hil/441/capture.bin
```

### Exit codes

`0` fine · `1` asked, nothing to report — an empty inbox, or a wait that ran
out · `2` hub unreachable · `3` cannot be carried out as asked · `130`
interrupted.

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
