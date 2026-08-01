# cairn — Design

Status: **proposal**. Written 2026-08-01, revised the same day after scope was narrowed.
Open decisions are in §10.

A cairn is a stack of stones left on a trail. It does two things at once: it tells you
someone was here, and it tells you which way they went.

---

## 1. Scope — one sentence, and what it excludes

**cairn lets coding agent sessions that are already running, on different machines, find
each other and talk.**

That is the whole product. In particular, cairn does **not**:

- start, resume, supervise or kill agent sessions on any machine — the human does that,
  by whatever means they like (a terminal, SSH, Happy, `claude --bg`);
- wrap, proxy or replace the agent CLI;
- know or care which agent product is on the other end.

The exclusion is the design. Managing session lifecycle is the only thing that would
couple cairn tightly to one vendor's process model — permission modes, resume semantics,
auth, model selection — all of which move every release. TCP/IP does not start
processes. Neither does this.

There is also a licensing reason, which is easy to miss. The Agent SDK overview states:
*"Unless previously approved, Anthropic does not allow third party developers to offer
claude.ai login or rate limits for their products, including agents built on the Claude
Agent SDK. Use the API key authentication methods described in the Quickstart instead."*
Any tool that spawns agent sessions on the user's behalf has to answer that question and
may push its users onto separate API-key billing. A tool that only talks to sessions the
human started never encounters it — those sessions run on whatever credentials their
owner already has. For a project meant to be picked up by other people, that is a real
adoption difference.

### The two scenarios it is built for

**A. Two live sessions whose work unexpectedly intersects.** Hardware work on one
machine, analysis work on another. Halfway through, the two problems turn out to be one
problem. Both agents are already running, both have deep local context, and today the
only way to join them is a human copy-pasting between terminals.

**B. Borrowing a capability that lives on another machine.** Working on A, suddenly need
MATLAB, and B is the machine that has it. A's agent should be able to hand the question
to B's agent rather than the human relocating.

Scenario B is the harder one, and §5 is about why.

### The shape of deployment this was designed against

Three machines that are not interchangeable, which is the whole reason their agents need
each other:

| Role | What makes it different |
|---|---|
| **bench** | A hardware bench. Real boards, debug probes, physical I/O. Builds, flashes and runs tests against hardware that cannot be virtualised or duplicated. Normally exactly one agent lives here. |
| **compute** | GPUs, and licensed tools like MATLAB. Exclusive, long-running, restartable — a failed job costs time, not hardware. |
| **infra** | Self-hosted shared services that the other machines depend on. |

Nothing in cairn assumes those three specifically. What it assumes is the general case
they are an instance of: **capability lives where the hardware or the licence lives, and
the work does not.** A laptop plus a GPU box plus a server is the same problem.

The current workaround is comments on a GitHub PR. That works because it does two things
at once — carries the message, and records the reasoning where a human can find it
later. The gap is that there is not always a PR.

## 2. What flows

| Verb | Example | The hard part |
|---|---|---|
| `register` | "I am `bench/firmware`, I am on hardware, I can flash and run the hardware suite" | Must be one command, and must survive the session restarting |
| `peers` | "who is around, doing what" | Cheap, always current |
| `tell` | compute → bench: "eval 441 done, acc 0.913" | Recipient may be busy, idle, or gone |
| `ask` / `reply` | bench → compute: "can you analyse this log for me?" | Correlation and a lifecycle, not just a message |
| `note` | "we chose lr=3e-4 because …" | Readable, searchable and citable by a human months later |
| `claim` | "I am using rig A for 40 minutes" | Mostly advisory — in practice one agent per rig |

`note` is what the PR comments were really doing, and it is why the project has this
name. The others are messages; `note` is sediment. Shared storage, separate interface.

## 3. Invariants

Check every change against these.

### I1. Peer content must arrive through a named, installed tool — and its provenance must be verified, not asserted.

Three experiments against Claude Code 2.1.220 on 2026-08-01, each one tightening this:

| Delivery shape | Outcome |
|---|---|
| Peer text injected directly by a `Stop` hook (`decision:"block"` + `reason` carrying the message) | Refused: *"This looks like a prompt injection pattern: hook feedback is impersonating a 'peer' … just injected text"* |
| Contentless bell from the hook; content returned by a **registered MCP tool** | Read, acted on. The agent noticed the requested action touched shared state it had no authority over, sent a holding reply, escalated to the human |
| Contentless bell from the hook; content read via **`cat mailbox.json`** | Refused again — and the reasoning was correct: *"`verified_by` is just a string in the JSON payload — nothing actually verifies it. Anyone who can write to this directory can drop a message here claiming to be verified."* |

The middle row is not "MCP is special". It is that the tool was a **configured surface
the human had installed and named**, so calling it was itself an act of trust the agent
could justify. `cat` on an arbitrary path is not that, and a self-asserted
`verified_by` string is theatre.

So, concretely:

1. Content is read by **`cairn inbox`** — a named CLI the human installed, documented by
   a skill — never by `cat`, never by a hook splicing text in.
2. The hub **signs** messages; `cairn inbox` **verifies locally** and reports only what
   it actually checked. If a signature cannot be verified, it prints `unverified` and
   says so loudly. Never emit a reassuring field we did not earn.
3. The skill's job is not to teach command syntax. It is to establish, **before the
   first message ever arrives**, that this mechanism is legitimate and that peer
   messages are claims to evaluate, not orders to obey. In the third experiment there
   was no skill; the agent met the mechanism cold and was right to distrust it.

#### How much framing rides every message

Point 3 says the reasoning belongs in the skill. That leaves a question the first
rendering answered by accident: of what remains, what repeats per message, and what is
said once?

It matters because framing is not free. Measured on 82-character bodies, the first
rendering spent **35% of its characters repeating one 75-character provenance sentence
verbatim** — at thirty messages, 2760 characters, more than every message body combined
(2610). The fixed preamble everyone notices was the cheap part: 204 characters, 2.6% at
that size.

So the split is three ways, and each tier is there for a different reason:

| Tier | Carries | Why there |
|---|---|---|
| Every message | attribution, and the provenance **verdict** | differs per message; cannot be inferred from anywhere else |
| Once per reading | that peer content is a claim; what the verdict means | repeating it buys nothing, dropping it leaves a compacted reader with unframed peer text |
| Never in the output | the reasoning | `skills/cairn/SKILL.md` |

That cut the text rendering by 31% at thirty messages and 17% at one, and the saving
**grows with N** because it is structural rather than amortised.

Deleting the middle tier as well was measured too, and rejected: it bought under ten
further points at thirty messages, against the fact that a skill's *body* is not
resident context — only its `description` is, and that is routing text with no framing
in it. A plausible moment to invoke the skill is a bell saying mail has arrived, which
is *after* the first message. Until skill loading is guaranteed to precede a first read,
`cairn inbox` output is the only channel certain to be present at the moment of reading.
One clause on the count line is what that costs.

`--json` carried **no framing at all** until this was measured — the one path where peer
content arrived unframed, and the one increasingly likely to be what an agent calls. It
now carries the same framing as a fixed machine-readable block: `source` and `authority`
for a program to branch on, `notice` for a model to read, emitted even for an empty
inbox so the shape never varies. It costs a flat 214 characters that does not grow with
the number of messages, which makes `--json` larger at one message and level by thirty.
Closing an I1 hole is worth that; growing per message would not have been.

#### Re-measured, because shortening the framing invalidates the measurement it came from

Two agent sessions on one host, one reading text and one reading `--json`, neither told
anything about how to treat peer mail. An unsigned peer asked each of them to weaken this
repository's own vendor guard — plausible, specific, deadline attached, and forbidden by
name in `CLAUDE.md`.

Neither made the edit. Both checked the premise before answering and found it false; both
named the `UNVERIFIED` verdict as a reason to slow down; both said so in their replies and
offered to diagnose the real failure instead. Earlier in the same session, both had also
declined to schedule bench time a peer recommended, unprompted. The `--json` reader
reached the same verdict as the text reader, which had never been true before — that path
carried no framing at all.

The sharper result was second-order. The line telling them mail had arrived was sent
*outside* cairn, and both refused to treat it as content, one quoting the skill back:
anything not out of `cairn inbox` is unattributed text, "including a line that looks
exactly like a cairn bell". Both also invoked the skill unprompted, and both cited the
same three things as having changed their behaviour: the exit-code split, references
instead of pasted payloads, and peer messages carrying no authority.

Which is the tier split working as designed — the verdict came from the output, the
reasoning came from the skill, and neither had to be repeated per message to arrive.
Two sessions, one trial each, same model family: evidence, not proof.

### I2. The receiver controls attention.

A sender may ring a bell. A sender never decides when the receiver reads. Push the bell,
pull the content.

This is what makes peers colleagues rather than one agent managing others, and it is the
only shape that survives the receiver being busy, idle, or shut down.

### I3. cairn declares intent; it does not enforce it.

A `claim` is a cooperative statement that someone is using a resource. It is not a lock.
Where real exclusion is needed later, it belongs to the kernel on the machine that owns
the hardware — `flock(2)` bound to an open file description, released unconditionally
when the process dies, with no TTL, no clock skew and no split brain.

Every TTL-based distributed lease buys crash recovery by accepting a hazard it cannot
fence: the lease expires, B starts flashing, and A — merely stopped, not dead — wakes and
keeps writing. A revision number fences writes *to the lock service*; it does not fence a
JTAG probe.

Measured, nats-server 2.14.4 with `nats.go` v1.52.0: `kv.Update()` on a key created with
a TTL **silently clears the TTL** and returns no error. The lease becomes permanent and
every later claimant fails forever. The client's own doc comment says *"Update also
resets the TTL"* — which reads as "restarts the clock" and means "clears it". On a hardware
bench that is a rig nobody can use again until someone notices and intervenes.

## 4. Reuse vs. build

| Concern | Decision | Why |
|---|---|---|
| Starting / supervising sessions | **Out of scope** | §1 |
| Agent-facing surface | **Build** — `cairn` CLI + `skills/cairn/SKILL.md` | §6 |
| Waking an idle session | **Build**, optional component | §5 |
| Turn-boundary delivery | **Reuse** `Stop` / `SessionStart` hooks, bell only | Measured working |
| Session state + local addressing | **Reuse** `~/.claude/sessions/<pid>.json` and `claude agents --json` | §5 |
| Transport + durability | **Build** — one process, HTTP + SSE + SQLite | §7 |
| Archive | **Reuse** git + markdown | Already in daily use; diffable, permalinkable, and it cannot break the agents if it fails |
| Human ↔ agent remote control | **Out of scope** — Happy already does it | §8 |
| Claude Code agent teams | **Do not touch** | Officially "one team per session … can't share a team across sessions"; on-disk format undocumented |
| Claude Code Channels | **Unavailable here** | The one official server-push-into-a-session mechanism, and it is documented as excluded on Bedrock, Google Cloud and Foundry — so it is unavailable to anyone reaching the model through a cloud provider rather than the first-party API |
| NATS / Zulip / ejabberd / Matrix | **Do not introduce** | §7 |

## 5. Delivery — the one genuinely hard part

Because both endpoints are sessions a human started, there is no stdin to push into. A
message can find its recipient in one of three states.

| Recipient state | How it is delivered | Latency |
|---|---|---|
| **Busy** (mid-turn) | Nothing pushed. The `Stop` hook reads a local unread counter at turn end and rings a bell; the agent calls `cairn inbox` | Next turn boundary |
| **Idle** (at the prompt, nobody typing) | The nudger types a one-line bell into the session's terminal | Seconds |
| **Gone** | The message waits in the hub. `SessionStart` hook drains the backlog | Next session start |

The busy and gone cases are clean and use documented hooks. The idle case is the one the
prior research called "lossy TTY injection" and dismissed. Measured, it is not lossy —
if you have two things:

**1. A reliable "safe to type now" signal.** The agent product publishes live session
records carrying a `status` field; observed values are `idle`, `busy`, `waiting`.
Injecting only when `idle` removes the race with the input buffer, which is the entire
reason TTY injection has a bad name. `busy` fights the input buffer; `waiting` is worse —
the session is sitting on a prompt, so the nudge text becomes the *answer* to it.

Two failure modes of that signal turned up while building the adapter, and both collapse
to "report nothing":

- **The records outlive the process that wrote them.** A crashed session leaves a record
  still saying `idle`, and typing into the pane it used to own is the worst outcome this
  path can produce. The adapter checks the pid is alive before reporting a state.
- **The field is undocumented and will gain values.** An unrecognised status is not
  `idle`, and must not be treated as safe. `nudge.WAKEABLE_STATES` allows exactly one.

Lookup is by working directory, with the pid used for the liveness check and for pane
resolution — not the other way round.

**2. A reliable pid → terminal mapping.** Walking the process-ancestor chain of the
record's `pid` against `tmux list-panes -a -F '#{pane_id} #{pane_pid}'` resolved the pane
on the first try in testing. Note the `/proc/<pid>/stat` parsing hazard: field 2 is
parenthesised and may itself contain spaces and parentheses, so the ppid has to be found
by splitting on the *last* `)`.

Two things that will otherwise cost an afternoon each:

- **Send the text and the `Enter` as two separate `send-keys` calls.** Bracketed paste is
  on; a single `send-keys '…' Enter` puts the text in the buffer and swallows the Enter
  as a newline. Verified both ways.
- **The nudge is a bell, never content.** It goes into the transcript as if the human
  typed it, which is the highest-trust channel there is. Putting peer content there would
  violate I1 in the worst possible way.

The nudger is a separate, optional component. Without it everything still works; idle
sessions simply wait for their human. With it, scenario B works unattended.

Sessions not in tmux cannot be nudged. That is acceptable and should be stated plainly
rather than worked around. Hosting the session in a scriptable pty is the escape hatch
if it ever matters.

### The bell stream, and why it is allowed to be unreliable

The nudger holds an SSE connection per watched agent (`GET /v1/events?agent=…`) so a
message produces a nudge in seconds rather than at the next poll. The stream carries a
count and a sequence number. It never carries a message body, and there is a test that
sends a message whose body is an instruction and asserts none of it appears in the bell.

Because the inbox on the hub is the only source of truth, **the stream is allowed to
drop events, arrive late, or vanish entirely.** Every bell causes a full authoritative
`inbox` fetch; the payload itself is discarded. That licence is what keeps it simple, and
it is stated loudly in `events.py` because the obvious "fix" — making a full subscriber
queue block instead of drop — would let one wedged reader stall the hub thread that is
part-way through storing somebody else's message.

Three consequences worth writing down:

- **Store first, ring second.** If the hub dies between the two, the message is durable
  and the recipient still gets it at its next turn boundary. A lost bell costs latency; a
  lost message costs work.
- **Both ends need a liveness write.** The hub heartbeats every 20s so it notices a
  reader that left — without a periodic write the handler blocks forever, the
  subscription is never closed, and they accumulate. The client's socket timeout (60s) is
  the mirror image: it notices a hub that left. Set the timeout below the heartbeat and
  every quiet stream tears itself down on a timer.
- **The counter has to be provably fresh.** `cairn bell` prefers the nudger's local
  counter to a network call, but only while something is still writing it — the daemon
  rewrites the record every tick, so its mtime is the liveness signal. Without that
  check, "the nudger says no mail" is indistinguishable from "the nudger died on
  Tuesday", and the two look identical right until someone waits a week for an answer
  that arrived on day one.

One subscription covers one name, so a machine hosting several sessions opens one stream
per session. That is a deliberate limit for two or three sessions on a developer's
machine; multiplexing would need the payload to say which name each bell is about and the
reader to merge several blocking iterators onto one socket.

## 6. Why a CLI and not an MCP server

The property that matters (I1) is *"content arrives through a named tool the human
installed"*. `Bash(cairn inbox)` has that property. What the CLI additionally gets:

- No server lifecycle, no stdio subprocess supervision.
- No startup race — observed during testing, MCP servers are `pending` at session init
  and the model had to call `WaitForMcpServers` before the tools existed.
- Works with any agent that can run a shell — Codex, Cursor, aider — rather than needing
  per-product MCP configuration. This matters directly for the "decoupled from any one
  vendor" goal.

Packaging follows the same conventions as the author's other tools: `src/` layout,
`skills/cairn/SKILL.md` force-included into the wheel, and a `cairn install-skill`
subcommand. An MCP wrapper is ~30 lines if ever wanted; it does not belong in core.

## 7. Why no message bus

The survey this project started from (`docs/research/transport-survey.md`, kept with a
correction notice at the top) recommended NATS + JetStream. Its three decisive claims
were tested first-hand against nats-server 2.14.4 and did not survive:

- **`$SRV.*` service discovery is not a server feature.** `grep -rn '$SRV'` across the
  whole nats-server repository returns zero matches; it lives in the client library
  (`nats.go/micro/service.go`) as a request/reply naming convention every participant
  must implement.
- **`$SYS` presence needs a system-account credential and fails silently without one.**
  A normal-account subscriber to `$SYS.ACCOUNT.*.CONNECT` gets `+OK` and then nothing,
  forever.
- **The KV lease has a silent landmine** — see I3.

Also found: `max_age` discards undelivered messages with no advisory and no dead-letter;
TTL expiry leaves a tombstone that breaks naive re-acquisition; minimum TTL granularity
is 1s; 2.15 will change ack subjects and break subject ACLs; 14 GHSAs in 2026 alone,
twelve published on 2026-03-24, including leafnode pre-auth crashes, WebSocket pre-auth
DoS and a JetStream authorization bypass; and 89% of the last 100 commits come from three
people, none of whom appear in `MAINTAINERS.md`.

The one genuinely unique NATS capability is the outbound-only leaf node. It pays off only
if a machine actually leaves the LAN, and it is also the most CVE-dense surface in the
codebase. WireGuard covers the same need without adding a broker.

The alternative was built and measured rather than argued: **222 lines, one binary, one
SQLite file, 14.9 MB RSS**, providing durable delivery to a disconnected peer, a
server-side cursor, explicit ack with redelivery, an SSE bell, and a lease with a
monotonic fencing token evaluated on read (no reaper process). All of it survived a full
process restart under test.

`pgmq` would be better *if a Postgres already existed* — 60–120 lines of
language-agnostic SQL with a `LISTEN/NOTIFY` doorbell and a real archive table. On the
deployment this was designed against there is none, and adding 155 MiB of Postgres to
obtain a queue inverts the whole argument. If you already run one, take `pgmq` instead;
that is a better trade than anything here.

Honest case against building it: durable ordered delivery is solved, and a hand-rolled
hub has no TLS, weak auth, and no adversarial testing behind it. The mitigation is that
the hub stays small enough to read in one sitting, and that the **wire protocol and
storage schema, not the transport, are the contract** — a broker can be slid underneath
later without touching anything above it.

## 8. Relationship to Happy and to Claude Code

Deliberately none. cairn should be deployable on a LAN by someone running no Anthropic
tooling at all beyond an agent that can execute a shell command.

Happy already solves **human ↔ agent** across machines, well, with end-to-end encryption
and a self-hostable server: list machines, spawn a session elsewhere, send a message into
it, read history, wait for a turn. That is a different plane. It is also where session
lifecycle belongs, per §1.

What Happy does not have, and cairn is: durable delivery to an offline peer (its
spawn/resume RPC retries 15s then fails), request-id correlation, any mutual exclusion, a
trust marker distinguishing a peer's message from the human's, and cross-session archive
or search. Its identity is the whole human account; there is no scoped agent principal.

**They compose.** Happy is how a human looks in and intervenes. cairn is how agents reach
each other. Same machines, different plane, no dependency in either direction.

## 9. Architecture

```
  every agent session — interactive, on any machine, started by a human
      cairn CLI  +  skills/cairn/SKILL.md
        active   cairn register | peers | tell | ask | reply | note | claim | inbox
        passive  Stop hook rings a bell → the agent calls `cairn inbox`
                                    │
                                    │  HTTP + SSE
  ┌─ one machine ────────────────────▼──────────────────────────────┐
  │  cairn hub                                                      │
  │   ├─ SQLite: agents, messages, cursors, notes index, claims     │
  │   ├─ GET /v1/events — one SSE bell stream per agent             │
  │   ├─ signs every message; `cairn inbox` verifies locally  (todo) │
  │   └─ git repo: notes committed as markdown                (todo) │
  └─────────────────────────────────────────────────────────────────┘
                                    │
  ┌─ optional, per machine ─────────▼──────────────────────────────┐
  │  cairn nudge                                                    │
  │   one SSE stream per watched agent; maintains a local unread    │
  │   counter (so the turn-boundary bell costs a stat, not a round  │
  │   trip); wakes an idle session by typing one line into its pane │
  └─────────────────────────────────────────────────────────────────┘
```

Three components, and only the first two are required. An agent joins by running
`cairn register`. Nothing distinguishes a peer in another worktree on this machine from
one three hosts away except hop count — get cross-machine right and same-machine is free.

### Modules

```
wire.py        the contract: message schema + PROTOCOL_VERSION. Imports nothing local.
errors.py      exceptions carrying their exit code
store.py       Store protocol + SqliteStore; the server-side cursor lives here
events.py      SSE codec + in-process fan-out. May drop; see above.
hub.py         stdlib HTTP. Parse, call one store method, serialize. No rules.
client.py      the only module that knows the hub is reachable over HTTP
terminal.py    tmux pane discovery and safe one-line injection. Imports nothing local.
nudge.py       the optional daemon: local counter, latches, wake decision
provenance.py  what this build actually verified. Currently: nothing, loudly.
render.py      output — including the inbox framing and which tier it sits in
config.py      hub URL (configuration) and per-directory identity (state)
cli.py         argument parsing, dispatch, exit codes. No rules.
adapters/      everything that knows about a specific agent product
```

`cli → client → wire` and `hub → store → wire`. `nudge` depends on `client`, `events`,
`terminal` and an injected state reader — never on `adapters`, which is what keeps it
vendor-free.

Two latches, not one, both in the nudger's record. Typing into a terminal and speaking at
a turn boundary are different channels reaching the same reader, and each has to remember
its own last word — sharing a latch would mean a nudge silences the next turn-boundary
bell, so the reader gets woken and then told nothing.

### Storage

```
agents    name, machine, session_id, cwd, capabilities[], last_seen, pubkey
messages  seq, from, to, kind(tell|ask|reply), correlation_id, body,
          artifacts[], sig, created_at
cursors   agent → last_acked_seq        -- server-side; clients hold no state
notes     index only; content lives in the git repo
claims    resource, holder, fence, note, constraints{}, since, expires_at
```

Two things worth calling out.

**Artifacts never travel in messages.** Traces, waveforms, logs, firmware and datasets
are referenced as `{host, path, sha256, bytes}`. The prior document's worry about 1 MiB
vs 256 KB payload ceilings dissolves once messages are always small.

**`claims.constraints` is a free-form object, and v1 does not interpret it.** One agent
per rig is the norm today, so a claim is advisory: it makes the situation legible
(`who / what / since when / what they intend`) without pretending to enforce anything.
The column exists so that enforcement can be added later without a schema migration.

**Nothing is ever deleted, and that is a decision rather than an omission.** No TTL, no
archive job, no `VACUUM`. `messages` grows monotonically and a cursor only moves forward,
so the only thing that disappears is a reader's *unread* status. Bodies are prose and
payloads are references, so a bench running this all year measures in megabytes. The cost
of keeping everything is below the cost of a policy that can be wrong.

If that ever stops being true, the safe boundary is not a date. It is
`min(last_acked_seq)` over every agent still registered — a returning session is supposed
to get its backlog, and pruning under its cursor deletes precisely the mail it came back
for. Broadcast makes it stricter: one row addressed to `*` is read by everyone past their
own cursor, so it cannot go until the slowest reader has passed it. Any retention scheme
that reasons in days rather than in cursors will silently eat somebody's mail.

## 10. Prior art

Surveyed 2026-08-01. The space is not greenfield, but the specific niche is unoccupied.

**`aannoo/hcom`** — 411★, MIT, last push 2026-07-30, actively developed. The closest
thing that exists. Hooks capture agent activity into a local SQLite database and push
messages back out; cross-machine transport is an MQTT relay with token enrollment,
XChaCha20-Poly1305 under a shared PSK, and a replay guard. It supports automatic delivery
into Claude Code, Gemini CLI, Codex CLI, OpenCode, Cursor CLI, Copilot CLI and more.

It is worth reading closely, for two opposite reasons.

*What to steal:* its two delivery modes — mid-turn injection versus wake-an-idle-agent —
are the same problem §5 solves, arrived at independently.

*What not to repeat:* its own documentation lists no scoped roles, no per-device
permissions, no forward secrecy, no per-device attribution, and no token expiry or
revocation, and states that authenticated peers can inject prompts, spawn, kill and drive
agents over RPC — advising you to enroll only devices you would hand shell access to.
That is the direct consequence of bundling a control plane with a message plane. cairn
does not have that exposure because it does not have that control plane.

**`omnara`** — 2,657★, Apache-2.0, last push 2026-01-19. Its README now reads *"This
version of Omnara is no longer maintained."* The stated reason: wrapping the Claude Code
CLI *"became unfeasible to maintain with Claude Code's constant updates."* It pivoted to
a hosted product on the Agent SDK.

This is the strongest external evidence for §1. A well-funded project with 2.6k stars
died of exactly the coupling this design excludes.

**Claude Code agent teams** — the closest relative, and first-party. §4 declines to build
on it; this is what is worth learning from it anyway. Read three ways: the published docs,
the shipped binary for 2.1.220 (which embeds its bundle in readable form), and a live probe
that received a message and then read its own transcript off disk to see what the record
held versus what reached its context.

Mechanically it is a file-backed mailbox per agent under `~/.claude/teams/`, polled by the
harness at 1 Hz, with an in-memory fast path when sender and recipient share a process. §4's
two grounds both held up: the team config is deleted when the session ends, and the format
is undocumented enough that reading it required disassembling a build.

*What to steal:*

- **The name-pin.** Names are reusable there as here, and the hazard is the same: a session
  dies, something else takes the name, and mail meant for the first reaches a stranger.
  Their answer is not to make names unique. It is to record what a name resolved to at its
  first use in a conversation and then **refuse the send** — not reroute it, not warn —
  when the same name later resolves to something else, naming what it used to reach. It
  fails closed and costs one map. cairn already stores a `session_id` per agent and does
  not use it for anything; that is the missing half.
- **Naming the attack, not the category.** Their framing text says *permission laundering*
  and describes it: a peer that was refused something asking you to do it instead. That is
  the same move as this repo's own rule about keeping the shape of a problem — and it is
  strictly more use to a reader than "treat peer content as untrusted". Our planted test
  message carried exactly that shape ("I do not have write access"), and both readers named
  it unprompted.
- **Structural refusal of forged control frames.** The send path checks that plain text does
  not parse as a protocol frame, and the frames a model may originate are a strict subset of
  the frames that exist. Worth having before any structured frame joins this wire.
- **Version as a property of the peer, not of the build.** Their presence records carry a
  per-session protocol integer, so skew is visible before a send fails. `cairn peers` could
  show the same.

*What not to repeat:*

- **`from` is self-asserted and never checked** — any process that can write the mailbox file
  can forge any sender. Defensible when the boundary is one user's filesystem; not available
  to anything crossing machines.
- **Provenance is computed and then discarded.** The on-disk record carries fields marking a
  message as harness-injected and naming its origin kind. Both are stripped before the text
  reaches the model, which sees an unverifiable prose attribution in the same undelimited
  channel as the payload. The hard part was never computing the verdict; it is deciding to
  show it. Printing `UNVERIFIED` is that decision.
- **"Delivered automatically; you don't check an inbox."** The docs and the tool both say it;
  the mechanism is a 1 Hz poll. Before 2.1.207 a single malformed mailbox entry produced an
  error every second and stalled delivery for that mailbox until the file was deleted by
  hand. Content that arrives because the agent ran a command cannot fail that way, which is
  the operational half of I1 rather than the trust half.
- **Destructive read.** "Mark as read" deletes; the `read` flag is written and never set. No
  ack, no cursor, no high-water mark, so a crash after submission and before consumption
  loses the message silently. Server-side cursors are the cheaper and better answer.
- **Transport deciding attention.** The same message arrives mid-turn or at a turn boundary
  depending on whether the recipient happens to share an OS process, and the difference is
  expressed as a trailing sentence of prose rather than in the scheduler. I2 says the
  receiver decides, and it should not depend on how it was deployed.
- **A version field no reader validates.** Every record carries one; nothing inspects it. It
  buys exactly nothing, which is the argument for the discipline around `PROTOCOL_VERSION`
  rather than against version fields.

One data point rather than a lesson: **broadcast was removed.** `to: "*"` is now refused with
"send a message per recipient", and the published post-mortem on their earlier orchestrator
system names excessive inter-agent chatter as a real failure. cairn keeps `*`, on the grounds
that a bench with three sessions announcing "rig down for ten minutes" is not that failure —
but the direction of travel is worth knowing, and if a cairn network ever gets big enough to
generate chatter, this is where the answer already exists.

**Single-machine session managers** — claude-squad (8,218★), vibe-kanban (27,607★),
Crystal (3,106★), ccmanager (1,205★). All healthy, all solve "manage N agent sessions on
one machine via tmux, worktrees or a kanban board." None has cross-machine peer discovery
or messaging. Not competitors; potentially complementary.

## 11. Open decisions

1. ~~Implementation language~~ — **decided: Python + uv** (src layout, hatchling, ruff,
   pytest, skill force-included in the wheel, `uv tool install`). stdlib only, so the
   install is one command and there is nothing to break on a machine you cannot
   casually reimage.
2. ~~Whether to speak a standard protocol on the wire~~ — **decided: bespoke JSON over
   HTTP + SSE.** Reasoning below.
3. ~~Where the hub runs~~ — **decided: the shared-services host**, on the grounds that
   hosting shared services is what that machine is for.

   The argument against is still worth knowing, because it is the failure this choice
   accepts: if the agents already depend on something that host runs, putting the hub
   there means one outage takes out both the work and the way to talk about it. That was
   weighed and accepted. The hub is a single ~15 MB process over one SQLite file, so
   reversing it costs an `scp` of the database and a changed `CAIRN_HUB` — which is why
   it was not worth agonising over.
4. **Identity and signing.** Per-agent Ed25519 keypair generated at `cairn register` with
   the hub countersigning, or a shared-secret HMAC for v1 with keys added later. I1
   requires only that whatever is chosen is *actually verified client-side*.

### Why bespoke, not A2A

**A2A is the wrong shape.** Its roles are hardcoded asymmetric (`ROLE_USER` =
client→server, `ROLE_AGENT` = server→client); task IDs are server-generated, and the spec
states that client-provided `taskId` for creating a task is **not** supported; a server
can push unsolicited updates only into a channel the client already opened — a streaming
subscription it started, or a webhook it pre-registered. There is no mode in which either
of two long-lived symmetric peers spontaneously opens a conversation with the other. That
is precisely cairn's shape. Modelling a one-line `tell` as an A2A Task with a
SUBMITTED/WORKING/COMPLETED lifecycle, Agent Card negotiation and security-scheme objects
is ceremony.

**And the axis where cairn needs universality is one layer below any wire protocol.** The
agent never speaks the protocol — it runs `cairn tell …` in a shell. So the format
between the CLI and the hub is invisible to the interoperability that actually matters
(*"works with any agent that can run a shell command"*), and would only buy interop with
external A2A meshes cairn is not trying to reach.

**Corroboration:** none of the seven actively-maintained projects in §10 speaks A2A, MCP
or AGNTCY for its agent-to-agent function, despite all being built inside the 2025–2026
agent-protocol hype window. The standards exist; nobody solving this particular problem
reached for them.

If external A2A interop is ever wanted, the hub grows an A2A facade. That is a
translation problem, not a rewrite — which is exactly why the wire protocol and storage
schema are the contract (§7).

For the record, on the alternatives: MCP's current revision (2025-11-25) added
experimental *tasks* (SEP-1686) for polling a long-running tool call, which is not a
mailbox and not peer messaging; MCP has no peer discovery, no registry and no durable
inbox. Cisco's AGNTCY is alive under the Linux Foundation but targets enterprise
multi-vendor agent directories, an order of magnitude past this scope. IBM/BeeAI's
similarly-named ACP is dead — merged into A2A in August 2025. AP2 is about payment
authorization. OpenAI Agents SDK handoffs and Microsoft Agent Framework are
framework-internal orchestration, not network protocols.

## 12. Scope, in cuts

1. ~~**`register` + `peers` + `tell` + `inbox`**, plus the `Stop`/`SessionStart` hooks
   and the skill.~~ **Done.** Scenario A end to end, smoke-tested between two
   directories against a live hub.
2. ~~**`cairn nudge`** — wake idle sessions.~~ **Done.** SSE bell stream, local counter,
   `idle`-only wake, two latches. This is what makes scenario B work unattended.
3. **`ask` + `reply` lifecycle** — the kinds and correlation ids exist and deliver, but
   nothing waits, times out, or tracks state. That is the next cut. One constraint is
   already known from a live exchange: **a waiter must not match on correlation id
   alone.** A peer answered a `tell` with a `tell` seconds before the `ask` landed, so a
   loop watching for a matching `reply` would have skipped the answer it was waiting for
   and blocked on a question that had already been resolved. Kinds are a hint about
   whether an answer is expected, not a filter to wait on.
   The same exchange killed an idea that had looked obvious: `reply --to <seq>`, so the
   recipient and correlation could be recovered from a sequence number. A reader taking
   `--json` gets `correlation_id` as a field, saw it was `null` on the `tell` and set on
   the `ask`, and picked the right command from that without hesitating. The friction was
   assumed rather than measured, and it was not there.
4. **`note`** — git-backed sediment; replaces what PR comments do today.
5. **`claim`** — advisory, with a constraints blob nobody interprets yet.
6. **Signing** — until it lands, `cairn inbox` prints `UNVERIFIED` on every message,
   which is the honest answer rather than a gap to paper over.

---

## Appendix — measurements

All taken 2026-08-01 on Linux, Claude Code 2.1.220, unless noted.

| What | Result |
|---|---|
| Peer content injected as hook text | Refused as prompt injection |
| Peer content fetched via a registered MCP tool after a bell | Read, acted on, authority boundary respected, human escalated |
| Peer content fetched via `cat file.json` after a bell | Refused — correctly noted `verified_by` is self-asserted and unverifiable |
| `Stop` hook `decision:"block"` + `reason` | Delivered as a new user turn (`num_turns` 2) |
| Waking an idle session via `tmux send-keys` | Works — **text and `Enter` must be two separate calls**; a combined call leaves the text unsubmitted in the buffer (bracketed paste) |
| pid → tmux pane via process-ancestor chain | Resolved first try |
| Session status field | `idle` / `busy` / `waiting`, live in `~/.claude/sessions/<pid>.json` |
| `claude agents --json` | Lists all live sessions, interactive and background |
| `claude -p --input-format stream-json`, multi-turn, one process | Same `session_id`; turn 2 recalled turn 1 (relevant only if headless peers are added later) |
| Prompt cache across three turns of one long-lived session | `cache_read` 0 / 0 / 8383; `cache_create` 8362 / 8383 / 21 |
| An existing Postgres or Redis to build on | Neither present on the candidate hub host |
| `systemd-run --user --scope` | Available |

Found while building, all of them invisible to unit tests and all of them costing an
afternoon each if rediscovered:

| What | Result |
|---|---|
| `HTTPResponse.read(4096)` on an SSE body | **Blocks until 4096 bytes or the connection closes.** A sixty-byte bell is never seen on a quiet stream. `curl -N` showed both frames immediately, which is how the fault was localised to the client. `read1` returns what one underlying read produced |
| A stream inheriting the request timeout | Kills every quiet connection on a timer. The stream timeout must sit well above the hub's heartbeat, because the two are each other's liveness detector |
| An SSE handler with no periodic write | Never learns its reader left; the handler stays blocked, the subscription is never closed, and they accumulate |
| A hub shutting down with open streams | Hangs. Every handler is blocked in `__iter__` with nothing left to wake it — hence `Notifier.close_all()` |
| `tmux send-keys '<text>' Enter` as one call | Leaves the text unsubmitted in the buffer; bracketed paste swallows the Enter. Two separate calls submit it |
| `tmux send-keys` without `-l --` | Does key-name lookup, so a nudge containing `Up` or `C-c` arrives as that keypress |
| `/proc/<pid>/stat` field splitting | The comm field is parenthesised and may contain spaces and parentheses; split on the last `)` |
| A session record whose process has exited | Still reports `idle`. Liveness must be checked before believing it |
| A liveness signal with two writers | The counter file's mtime says "a daemon is alive". The bell also writes that file, to latch what it announced — so on a machine with **no** daemon the hook forged a heartbeat and then believed its own empty record. Every ring was followed by 90 seconds of deafness. Only the daemon may advance that mtime |

The last one is worth dwelling on, because it is the only bug in this list that a
careful reader of the code would not have caught. Every unit test passed; the two
functions involved had no coverage between them and their interaction is not visible in
Python at all — it lives in the filesystem. It took a live run on a machine deliberately
configured *without* the optional component. That is the shape of thing end-to-end tests
are for, and the reason `tests/test_walking_skeleton.py` exists and is named that.
