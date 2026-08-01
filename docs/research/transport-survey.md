# Transport survey — cross-machine agent communication

Research date: 2026-08-01. Findings were gathered by direct fetch of source repos,
official docs, package registries and the GitHub / HN Algolia APIs.

> **Read the correction notice below before using anything here.** This document is the
> *input* to cairn's design, not its conclusion. Several of its central claims were
> tested first-hand afterwards and did not survive. `docs/design.md` is the authority;
> this file is kept because the survey work is reusable and because a rejected option
> with its reasoning intact is worth more than a rejected option remembered as a hunch.

## Correction notice

Three claims below were checked against a running `nats-server` 2.14.4 with the official
`nats.go` v1.52.0 client, and are **wrong**:

| Section | Claim as written | What testing found |
|---|---|---|
| L4, service discovery | *"NATS ships `$SRV.PING` / `$SRV.INFO` / `$SRV.STATS` — zero-code discovery"* | Not a server feature. `grep -rn '$SRV'` across the whole nats-server repository returns **zero matches**; it is a request/reply naming convention in the client library (`nats.go/micro/service.go`) that every participant must implement |
| L4, presence | *"Zero client code. ~40–60 lines total"* | Requires a **system-account credential**. A normal-account subscriber to `$SYS.ACCOUNT.*.CONNECT` receives `+OK` and then nothing, forever — a silent failure |
| L4, leases | *"TTL can only be set at `create`… renewal requires delete-then-create, which opens a brief window"* | The race described does not exist — atomic CAS + TTL renewal works via `js.PublishMsg` with `WithExpectLastSequencePerSubject` and `WithMsgTTL`. But the real hazard is worse and is not mentioned: **`kv.Update()` silently clears the TTL** and returns no error, turning a lease into a permanent lock. The client's own doc comment says *"Update also resets the TTL"*, which reads as "restarts the clock" |

Also missing from the survey, and material:

- `max_age` **silently discards undelivered messages** — no advisory, no dead-letter.
- TTL expiry leaves a tombstone that breaks naive re-acquisition; minimum TTL granularity
  is 1 second.
- 2.15 will move ack subjects behind `js_ack_fc_v2`, **breaking subject ACLs**.
- The CVE row understates 2026: **27 unique GHSAs, 14 of them in 2026, twelve published
  on a single day (2026-03-24)** — including three leafnode and three WebSocket pre-auth
  issues, and a JetStream authorization bypass. §3 below eliminates Matrix partly for
  taking 11 advisories in one day; the same standard was not applied here.
- Bus factor is worse than stated: the last 100 commits are 89% from three people, **none
  of whom appear in `MAINTAINERS.md`**.

The survey's *shape* held up. Its conclusion did not: the layering in §2 is still the
most useful thing here, but the recommendation in §5 was not adopted. See
`docs/design.md` §7 for what was built instead and why.

---

Evidence marks used throughout:

- **[V]** verified first-hand during this research
- **[R]** on the record from a named party (real quote; substance not independently confirmed)
- **[I]** inference / estimate

Known blind spots, stated up front:

- `reddit.com` returned 403 from this network. Reddit data came only from the
  `arctic-shift.photon-reddit.com` mirror and is thinner than the rest.
- The GitHub core API quota was exhausted mid-research. The star count for
  `aannoo/hcom` is **not** verified; its README content is.
- NATS "<20 MB RAM at rest" is an official claim **[R]**; measured separately afterwards
  at 20.7 MB RSS with JetStream enabled and two streams.

---

## 1. Problem statement

Several development machines on one internal network, each specialised — a hardware
bench, a compute box, a host for self-hosted services — with a coding agent running on
each. The agents need to talk to each other.

A workable stopgap is leaving comments on a pull request. That does two things at once:

1. **carries the message**, and
2. **records the reasoning for an important problem** in a form a human can read later.

The gap is that there is not always a PR.

### Constraints this survey was run under

| # | Constraint |
|---|---|
| C1 | Minimise dependency on external services |
| C2 | Self-host on an internal machine if at all possible |
| C3 | If external is unavoidable, end-to-end encryption where the relay cannot read plaintext is acceptable |
| C4 | Deployment shape: agent nodes get a CLI tool plus a skill (register, query, targeted send); one node runs storage/relay |
| C5 | Selection criteria: lightweight, extensible, robust, able to mature; plus project activity, community heat, maturity |
| C6 | Operators already have shell access to every host, so human-driven nudges are cheap and currently acceptable |
| C7 | Model access is through a third-party cloud provider rather than the first-party API |

C7 matters more than it looks. See §2, L5.

---

## 2. Layer decomposition

The single most useful result of this research is that "cross-machine agent chat" is
not one problem. It is eight layers, and they have very different maturity. Most of
them are commodities with several good answers. Two of them have no off-the-shelf
answer at all.

```
  L7  Archive / human readability      ← half of why PR comments worked
  L6  Agent adapter (CLI + skill)      ← ~all of the actual work
  L5  Delivery seam into a session     ← THE REAL GAP
  L4  Coordination primitives          ← CAS, leases, presence
  L3  Identity & authorization
  L2  Durability, ordering, replay
  L1  Transport / bus
  L0  Reachability
       ─────────────────────────
  L8  Trust posture (cross-cutting)
```

---

### L0 — Reachability

Getting a handful of hosts (several on one network, possibly one outside it) into one addressable space.

| Candidate | Verdict | Notes |
|---|---|---|
| **Plain LAN, no overlay** | Sufficient today | The machines are already on one network; an off-network host is the only case that needs more. |
| **WireGuard** | Good fallback | Kernel-space, tiny, no external dependency. Manual key distribution. |
| **Tailscale** | Violates C1 | Control plane is a SaaS. |
| **Headscale** | Acceptable | Self-hosted Tailscale control plane; more moving parts than raw WireGuard at this scale. |
| **NATS leaf node** *(app-layer)* | **Best fit** **[V]** | Leaf **dials outbound only; the hub never dials back**. Zero port-forwarding and zero DDNS on the internal side. `remotes` supports `ws://` (so it can ride 443) plus mTLS and an HTTP proxy. |

**Finding.** The NATS leaf-node model collapses L0 into L1. If every machine stays
on one network, put the hub on one of them; leaf nodes are the escape hatch for the day
one of them moves off-network. No other candidate transport
offers this — XMPP s2s and Zulip both require a mutually reachable listening port.

**Caveat [V]:** the NATS leafnode port (7422) has a history of pre-auth crash CVEs
(5 leafnode advisories, some marked "incomplete fix"). If it is exposed to the public
internet it must be behind mTLS or inside WireGuard. Never bare.

---

### L1 — Transport / bus

| | **NATS + JetStream** | **ejabberd (XMPP)** | **Ergo (IRC)** | **Zulip** | **Matrix** |
|---|---|---|---|---|---|
| Image / footprint **[V]** | **6.6 MiB** scratch, no DB | ~1 svc, no DB | single Go binary | **1.07 GiB**, 5 svcs, needs PG + RabbitMQ + redis + memcached | Synapse: PG mandatory |
| RAM at rest | <20 MB **[R]** | modest | tiny | **2 GB min** **[V]** | high |
| Max payload **[V]** | **1 MiB** (8 MiB rec., 64 MiB hard) | 256 KB stanza | ~350–400 B/line | 10,000 chars | 64 KB event |
| Compose lines **[V]** | 1 service | ~7 | ~5 | **112** | many |
| 90d authors **[V]** | 25 (**4 = 92%**) | 6 + ProcessOne | **1** | **53** | varies; forks 1–2 |
| Governance **[V]** | CNCF Incubating; **trademarks now held by Linux Foundation** | ProcessOne (commercial) | single maintainer | Zulip Foundation (non-profit, since 2026-05) | Foundation in deficit |
| CVEs, last 5y **[V]** | 36 total (8 MQTT = 0 if disabled; 5 leafnode) | **effectively 0** (last real one 2014) | few | 56 CVE ids in changelog (healthy process) | Synapse: **11 advisories in one day**, 2026-07-28 |
| Federation | leaf/gateway | s2s | server links | **none** **[V]** | yes (and a liability) |

Also considered and rejected early:

- **Redis Streams** — pub/sub and Streams are two disjoint mechanisms; no subject-level
  ACLs; no leaf nodes; CAS is only `SET NX`/Lua with no revision semantics. Plus the
  2024-03 RSALv2/SSPL relicense actually happened **[V]** (Valkey forked under LF).
- **Openfire** — **disqualified.** Its REST plugin can only broadcast to *all online
  users*; there is no send-to-one-user or send-to-one-room endpoint **[V]**. Also 46
  CVEs including CVE-2023-32315 (8.6, unauthenticated admin-console bypass affecting
  every version since 2015, exploited in the wild by mining botnets).
- **Matrix** — **eliminated.** Reasons in §3.
- **git-as-bus** — appealingly dependency-free, but polling latency and merge conflicts
  make it a poor coordination substrate. It re-enters at L7 as an *archive*, which is
  what it is actually good at.

---

### L2 — Durability, ordering, replay

This is where the **idle-agent problem** bites: an agent sitting at its prompt is not
executing, so it cannot poll. Whatever arrives while it is idle must survive.

| Candidate | Mechanism | Client-side work |
|---|---|---|
| **NATS JetStream durable consumer** **[V]** | Cursor stored **server-side**. Disconnect for days, reconnect, resume from the cursor. Set `max_age: 30d`. | **Zero.** No local state at all. |
| **XMPP MAM (XEP-0313)** | Server archive, queryable by range | Moderate |
| **IRCv3 CHATHISTORY** | Server-side scrollback | Moderate |
| **Zulip event queue** **[V]** | `DEFAULT_EVENT_QUEUE_TIMEOUT_SECS = 600` — **GC'd after 10 min idle**. Queues live in Tornado **process memory**; a server restart evaporates all of them. | **High.** `BAD_EVENT_QUEUE_ID` → re-register → anchor-backfill via `get-messages` (≤1000/call) is a *mandatory* path, not an optimisation. ~1.5–2 person-days **[I]**. |
| **Matrix sync token** | Works | Moderate |

**Finding.** NATS wins this layer outright, and it wins it on the exact failure mode
that matters. Zulip's message *history* is permanent and complete — but its *event
delivery* is the weakest of the five.

**Trap [V]:** ejabberd's MAM and MUC archiving are **on by default** in the shipped
config. Prosody's are not, and with no archive store configured Prosody **silently
degrades to in-memory archiving** — restart loses everything. Default expiry 1 week,
cap 10,000 messages.

**Trap [V]:** ejabberd's default Mnesia backend is documented as corrupting around 2 GB.
Switch MAM to SQLite or Postgres.

---

### L3 — Identity & authorization

| Candidate | Model | Granularity |
|---|---|---|
| **NATS Operator/Account/User JWT** **[V]** | Three-tier Ed25519 JWT. A user's permission set *is* two subject lists: publish and subscribe. | **Per-agent, per-subject, cryptographically enforced.** An agent literally cannot publish outside its grant. |
| **ejabberd `api_permissions`** **[V]** | ACL by *who* × *which command* × *which transport* (e.g. allow HTTP→`send_message`, deny HTTP→`stop`) | Per-command |
| **Ergo** | oper blocks, ChanServ ACLs | Per-channel |
| **Zulip** | bot users, realm roles | Per-user |
| **Matrix** | power levels | Per-room |

**Finding.** This layer is where NATS quietly delivers something the others cannot, and
it is the reason the L8 trust question stops being blocking. Issuing a JWT that permits
`publish → agent.inbox.compute-1` and `subscribe → agent.inbox.bench-1` and nothing else
is *configuration*, not code. ejabberd's `api_permissions` is the closest competitor but
scopes commands rather than destinations.

---

### L4 — Coordination primitives

The distinction that took the longest to pin down: **messaging systems fan out and
converge; coordination stores arbitrate and return failure.** Atomic task claiming needs
a conditional write *that reports the conflict*. Broadcasting "I claim task 7" to a
channel is not that — two agents can both broadcast it.

#### Compare-and-swap

| Backend | CAS | Detail |
|---|---|---|
| **NATS KV** | ✓✓ **real** **[V]** | `create(key,val)` = CAS against revision 0; fails if the key exists. `update(key,val,rev)` = CAS against a read revision; on mismatch the write is **rejected and nothing is overwritten**. Server error **10071 `wrong last sequence`**. Consistent across languages: Go `ErrKeyExists`, Python `KeyWrongLastSequenceError`, JS `StreamWrongLastSequence`. CLI: `nats kv update BUCKET key value <REVISION>`. |
| **XMPP PubSub** | ✓ **spec-mandated** **[V]** | XEP-0060 *MUST* return `<conflict/>` on duplicate node creation. Verified in Prosody `util/pubsub.lua:606`. Also `publish-options` → `precondition-not-met`. |
| **Ergo IRC** | ✓ **accidental** **[V]** | `/CS REGISTER #task-<id>` is genuinely atomic (channel registration is serialised under `stateMutex`). Works, but it is a side effect of the implementation, not a contract. |
| **Zulip** | ✗ (hack) **[V]** | No CAS anywhere in the message layer. Bot storage is `update_or_create` in a *per-bot private namespace* — not even eligible as a shared lock. The one race-free primitive is `POST /user_groups/create`, which hits a DB-level `unique_together (realm, name)`. Usable, but a hack, with no TTL. |
| **Matrix** | ✗ **[V]** | State events silently overwrite. |

#### Leases (auto-expiring claims)

**Only NATS has this.** KV per-key TTL, available since nats-server 2.11 (2025-03). A
crashed claimer does not deadlock the task forever.

**Important limitation [V]:** TTL can only be set at `create`. `put`/`update` do **not**
accept `--ttl`. Renewal requires delete-then-create, which opens a brief window where
another claimant can slip in. Any heartbeat-renewal design must handle this.

XMPP and Ergo can achieve mutual exclusion but **cannot expire it** — an orphaned lock
needs manual reaping.

#### Presence

| Backend | Mechanism | Quality |
|---|---|---|
| **NATS** | `$SYS.ACCOUNT.*.CONNECT` / `DISCONNECT` server-side advisories **[V]** | **Zero client code.** ~40–60 lines total. |
| **XMPP** | Native presence, extensible with a custom namespace (RFC 6121 §4.7.3) | Good, rich |
| **ejabberd MUC/Sub** | `subscribe_room` — receive room traffic **without maintaining presence** **[V]** | Excellent for flapping agent connections. No Openfire equivalent. |
| **IRC** | MONITOR, always-on clients | Adequate |
| **Zulip** | Native API but only `active`/`idle`; `OFFLINE_THRESHOLD_SECS=200` **[V]** | **Up to 200s stale.** Since 7.0 it no longer distinguishes reporting clients, so each node needs its own bot user. |

#### Service discovery

NATS ships `$SRV.PING` / `$SRV.INFO` / `$SRV.STATS` — **zero-code discovery** **[V]**,
which directly satisfies the "register / query" half of C4.

---

### L5 — Delivery seam into a running session

**This is the layer with no good off-the-shelf answer, and it is Claude Code-specific.**
Everything above is a commodity. This is not.

The real axis is not push-vs-pull as transports. It is **who owns the interrupt**:

- **Scheduling authority sits with the sender** → that is *management*. One agent
  directs another.
- **Attention is receiver-controlled** → that is *peer collaboration*. Two workers,
  each finishing their own thing.

The intuition that push feels like one agent managing others while pull feels like two
people working independently is exactly right, and it is a statement about authority,
not about protocol.

#### The four actual seams

| Seam | Timing | Fidelity | Notes |
|---|---|---|---|
| **`Stop` hook** | Turn boundary | Clean | **The best option.** Fires when the agent finishes a turn. Deliver here with a "drained" latch so a delivery doesn't retrigger itself. |
| **`SessionStart` hook** | Session open | Clean | Drains the backlog accumulated while the session was down. Pairs with `Stop`. |
| **TTY injection** (`tmux send-keys`) | Mid-turn | **Lossy** | Races the input buffer. Works, but not a foundation. |
| **SDK / headless** | Arbitrary | Total | You own the input stream. Requires giving up the interactive TTY. |

Everything else that calls itself "push" is pull with a nag attached.

#### Official options, and why they don't close it

- **Agent Teams** (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) — same-machine only.
  Does not address the cross-machine case at all.
- **Channels** (research preview) — standardises push-into-a-running-session, which is
  exactly this layer. **But it is explicitly unavailable on Amazon Bedrock, Google
  Cloud's Agent Platform, and Microsoft Foundry.** Given C7, it is unavailable.
  This is the single strongest justification for building a delivery layer rather than
  waiting for one.

#### Recommended shape

> **Push the bell, pull the content.** A durable ordered store is the source of truth;
> a lightweight signal says "there is mail"; the agent fetches at a turn boundary.

This keeps receiver-controlled attention (peer semantics) while eliminating the
idle-agent failure mode, because the store persists regardless of who is awake.

---

### L6 — Agent adapter (CLI + skill)

This is where essentially all the implementation effort lives.

| Backend | Est. effort **[I]** | Language notes **[V]** |
|---|---|---|
| **NATS** | 700–900 lines Go, **2.5–4 person-days** | Use **Go**. `nats.go` v1.52.0 is stable. JS and Python are mid-major-version churn: npm `nats` 2.x frozen since 2025-03 (yet still 2.5× the downloads of the new `@nats-io/*`), Python `nats-key-value` is at 0.1.0. **Online tutorials do not match current syntax.** |
| **ejabberd** | 200–400 lines Python, 1–2 days | REST does most of the work — see below. slixmpp is healthy, but its real repo is on **Codeberg**; the GitHub one is a dead mirror. |
| **Zulip** | **5–6 person-days**, 1.5–2 of them purely on event-queue recovery | Python `zulip` 0.9.1 is active. `zulip-js` is stalled at 2024-10-22 (with a 46-month gap before that). **No official Go binding.** |

**ejabberd's REST API is the standout for C4** **[V]**: `mod_http_api` is enabled by
default and exposes ~224 commands over HTTP, `ejabberdctl` and XML-RPC identically.
The two that matter:

- `send_message` — targeted send, including groupchat
- `send_stanza` — inject **arbitrary raw XML** with a chosen `from`

A single `curl` sends a message. Almost no client code required.

---

### L7 — Archive / human readability

Half of why PR comments worked. Easy to under-weight.

| Candidate | Strength | Weakness |
|---|---|---|
| **Zulip topics** | **Unmatched.** stream+topic is natively "one issue, one thread". Topics resolve with a ✔ (= issue open/closed). Canonical permalinks (`#narrow/channel/42-slug/topic/issue-1234/near/98765`) paste into GitHub issues. Full search-operator DSL over PostgreSQL FTS. Four first-party clients **including a terminal TUI**. | 1.07 GiB and 2 GB RAM to get it |
| **Git-backed markdown** | Uses tooling already in play. Searchable, permalinkable, reviewable, diffable, free backup. ~100 lines **[I]** to dump from the bus on a timer. | Not live; latency to archive |
| **SQLite FTS5 mirror** | Purpose-built, off the critical path, rebuildable from JetStream | 500–800 lines **[I]** of UI you now own |
| **Any XMPP/IRC client over MAM/CHATHISTORY** | Free with the transport | No thread model; search is client-dependent |
| **NATS native** | — | **Hard failure.** `nats stream view` pages at ≤25, **no full-text search, no thread view**. NUI (651 stars) is unofficial. |
| **Status quo: PR comments** | Already works | Requires a PR to exist |

**Finding.** NATS's archive gap is a *UI* gap, not a *data* gap — the content is already
durably in JetStream. That asymmetry matters: bolting an archive onto NATS is additive,
rebuildable, and off the critical path. Bolting distributed locking onto Zulip is not.

---

### L8 — Trust posture (cross-cutting)

A peer's message is **a claim to verify, not a command to obey.** An agent that treats
inbound peer text as instructions is one compromised or merely confused peer away from
executing arbitrary work.

`aannoo/hcom` — the closest existing prior art — states this limitation in its own
README **[V]**: no scoped roles, no read-only peers, no per-device permissions, and
"prompt injection from an authenticated peer" is listed as a known accepted limitation.

Three mechanisms, in increasing cost:

1. **Scoped identity with capability boundaries** — an agent can only send where its
   grant permits. **NATS gives this for free at L3** (subject-scoped Ed25519 JWTs);
   ejabberd gives a per-command approximation.
2. **Message-as-data framing** — the adapter renders inbound peer messages inside an
   explicit envelope that marks them untrusted, rather than splicing them into the
   prompt as if the user said them. This is an L6 concern and costs almost nothing if
   designed in from the start.
3. **Human approval gate** — high-consequence actions triggered by a peer message
   require confirmation. Given C6, this is cheap.

**Key consequence:** because mechanism 1 is *configuration* under NATS rather than code,
the "do I need to defend against my own prompt-injected agent?" question is **no longer
blocking**. Start with least-privilege grants; adding 2 and 3 later is additive, not a
rewrite.

---

## 3. Why Matrix was eliminated

Recorded because it was a serious candidate for most of the research.

**Institutional.** The Matrix Foundation's first public annual report (FY2025) **[V]**:
£910,821 in costs, **£310,596 loss**, no grants received, Managing Director post vacant,
and — verbatim — *"We churned a Gold member, and the one left (Automattic) now
corresponds to 50% of our revenue"*, *"the Foundation is still disproportionately
dependent on Element's in-kind donations and financial support, which is
unsustainable"*, and *"the lack of budget is hindering the progress of Matrix and
putting its adoption at risk."* Break-even has been the #1 objective two years running.
Element now runs matrix.org itself on the **proprietary** Synapse Pro as a cost-cutting
measure.

**Synapse operationally.** PostgreSQL mandatory; `state_groups_state` is append-only and
*"never automatically cleaned up, and grows in size infinitely"*; **users cannot be
deleted — the API has no such option**; no admin panel ships; federation cannot be
cleanly disabled. On 2026-07-28 it took **11 security advisories in a single day** (5+1
high). The mitigating signal is real, though: releases every ~2 weeks, all responsibly
disclosed and batched.

**The Rust forks.** conduwuit was archived 2025-04-11 and split acrimoniously into
tuwunel and continuwuity. *Correction to an earlier statement in this research:* the
original author (June Clementine Strawberry) works on **tuwunel**; **continuwuity is the
fork without the original author.** Bus factors are 1 and 2 respectively (tuwunel:
jevolk = 77.7% of 90d commits). Both share a critical-CVE lineage: **two criticals in
three months, both enabling event-signature forgery** by an unauthenticated remote
attacker. CVE-2026-24471 was **exploited in the wild against continuwuity.org itself**;
the promised postmortem (due 2026-01-31) is still unpublished **[V]**.

**Dendrite** — treat as dead. Last release v0.15.2, 2025-08-15; YunoHost flagged it
`deprecated-software`.

The one genuinely positive Matrix datapoint: the conduwuit lineage is now **16.7%** of
the federation (continuwuity 8.6% + tuwunel 8.1%, ~7.1× growth each since 2025-10),
against Synapse falling 86.6% → 78.2% **[V]**, via `api.matrixrooms.info/stats`.

---

## 4. Notes on the other backends

### ejabberd beats Prosody (revision to an earlier conclusion)

The XMPP camp's answer is **ejabberd**, not Prosody:

- **Prosody has no first-party Docker image.** `prosody/prosody` on Docker Hub has been
  stale since 2021-05; the project points container users at Snikket **[V]**. Against C4
  (Docker Compose) that is disqualifying on its own. `mod_rest` is community-only.
- **ejabberd**: 7-line compose, one service, **no database** (embedded Mnesia), no setup
  wizard, arm64 images, env-var admin bootstrap.
- **Security**: ejabberd has 10 CVEs all-time and **effectively zero real ones in five
  years** (the last genuine defect was 2014; the 2020 entries are Zyxel products that
  embedded it). Prosody has 23 total, ~6 in five years, 4 Medium in 2026.
- **Bus factor 6 + commercial backing** (ProcessOne; the top two committers are
  employees), 5–6 releases/year. The only candidate not resting on one or two people.
- ProcessOne has added an **"AI Agents — Human-AI at scale"** product line (marked
  Coming soon) — they are moving toward this use case.

### Ergo

Single maintainer, but genuinely good: single Go binary, `/CS REGISTER #task-<id>` gives
accidental-but-real atomicity, and there is strong community precedent — a 2026-03
Show HN at **340 points** describes exactly this scenario (cross-machine dual agents
coordinating over a private `#backoffice` channel). The hard ceiling is the **~350–400
byte line limit**, which makes structured payloads painful.

### NATS — the trademark dispute is resolved

The main historical risk against NATS is closed. **2025-05-01, CNCF press release [R]:**
Synadia transferred **both NATS trademark registrations to the Linux Foundation**; the
nats.io domain and GitHub repos remain with CNCF; **Apache-2.0 continues**; and Synadia
must **rename** any closed-source fork. Verified downstream **[V]**: 2.12 (2025-09) and
2.14 (2026-04) shipped normally, and Synadia's commercial products are all renamed.
NATS remains CNCF **Incubating** (since 2018-03-15, never graduated), so the original
single-vendor-dominance concern is not fully retired.

There is also an official **Synadia Agent Protocol** (nats.io blog, 2026-05-25):
`$SRV.PING.agents` discovery, `agents.hb.*` heartbeats at 30s with 3-miss timeout,
streaming typed JSON chunks. It maps almost line-for-line onto the requirements here.
**But** it lives in the `synadia-ai` org rather than `nats-io`, is outside CNCF
governance, was created 2026-04, and has ~70 stars. **Reference design, not a
dependency.**

### Zulip

Eliminated as a *primary* backend on weight (1.07 GiB image / 5 services / 2 GB RAM /
zero federation / no CAS / 10-minute event-queue GC), but it is the undisputed L7
winner and remains viable as an archive-only component.

Governance footnote **[V]**: founder Tim Abbott (10,895 commits) plus three core
developers left for **Anthropic** on 2026-05-15; Kandra Labs was donated to the new
non-profit Zulip Foundation with 12 full-time maintainers remaining. Abbott predicted a
slowdown; git says it has not happened yet (161 / 267 / 241 commits in May / June /
July). Apache-2.0 with no CLA plus foundation ownership makes a rug-pull *less* likely
than before.

### Prior art: `aannoo/hcom`

The closest thing that already exists **[V, README only — star count unverified]**:
cross-machine MQTT relay, self-hostable broker, E2E encryption, presence/idle tracking,
**both** mid-turn injection and idle wake. It solves L0–L6 to a usable standard.

What it does not solve is L8, by its own admission (see §2 L8). **The remaining gap in
this whole space is trust and authority, not transport.**

---

## 5. Candidate architectures

### Option A — NATS coordination + git-backed markdown archive ★ recommended

```
L0/L1  nats-server, hub on a LAN machine (leaf nodes if a host leaves the LAN)
L2     JetStream stream, max_age 30d, durable consumer per agent
L3     Operator/Account/User JWTs; per-agent publish/subscribe subject grants
L4     KV create() for claims + per-key TTL leases; $SYS advisories for presence
L5     Stop hook (drain w/ latch) + SessionStart hook (backlog)
L6     Go CLI: register / query / send  +  a skill wrapping it
L7     Timed job: dump stream → markdown → commit to a git repo
L8     Least-privilege JWTs from day one; untrusted-envelope framing in the adapter
```

**Cost [I]:** 2.5–4 person-days for the adapter, ~100 lines for the archive dumper.
**Runtime:** one 6.6 MiB container.

**Why.** It wins L0 (outbound-only leaf), L2 (zero-client-state replay), L3
(subject-scoped crypto identity), L4 (the only real CAS *with leases*) — and its single
weakness, L7, is a missing UI over data that is already durable. The git archive
reproduces the specific thing that made PR comments work, using tooling already in daily
use, and it cannot break the agents if it fails.

**Risks.** Bus factor 4 (derekcollison / kozlovic / neilalexander / MauriceVanVeen =
92% of 90d commits). Only third-party audit is Cure53, **2019** — seven years stale.
Must pin `max_file_store` explicitly (it defaults to **75% of host disk free space**,
which is wrong inside a container). Write the CLI in Go.

---

### Option B — NATS coordination + SQLite FTS5 mirror

Identical to A, except L7 is a purpose-built read-only mirror (JetStream → SQLite FTS5),
500–800 lines **[I]**.

**Choose over A if** the archive needs a real query interface rather than
grep-over-a-repo, and owning a small UI is acceptable.

---

### Option C — ejabberd, single system

```
L0/L1  ejabberd, 7-line compose, no database
L2     MAM + MUC archiving (on by default; switch storage to SQLite, not Mnesia)
L3     api_permissions ACLs, per-command, per-transport
L4     XEP-0060 <conflict/> for claims (mutual exclusion, but NO leases)
L5     Stop / SessionStart hooks, same as A
L6     ~200-400 lines Python over the REST API (send_message / send_stanza)
L7     Any XMPP client reading MAM
L8     api_permissions + untrusted-envelope framing
```

**Cost [I]:** 1–2 person-days — the lowest of any option, because `send_stanza` over
plain HTTP removes most of the client code.

**Why.** One system, nothing to bolt on. Best security record in the field. Real
commercial backing and the healthiest bus factor. Human-readable through any XMPP client
without extra work.

**Trade-offs.** Mutual exclusion without expiry — orphaned locks need manual reaping.
256 KB stanza cap vs NATS's 1 MiB. Needs a reachable listening port, so a host that
leaves the LAN needs WireGuard (no outbound-only mode). Authorization scopes *commands*,
not *destinations*, so the L8 story is weaker than NATS's.

---

### Option D — NATS coordination + Zulip discussion

Two systems: NATS for L2/L3/L4 machine coordination, Zulip for L7 human-readable
discussion (and human participation via mobile/desktop/TUI clients).

**Cost [I]:** 2.5–4 days (NATS) + 5–6 days (Zulip, half on event-queue recovery).
**Runtime:** ~1.1 GiB and 6 containers.

**Choose only if** *humans routinely participating in agent discussions* is a top-tier
requirement — @-mentioning an agent from a phone, ticking ✔ on a resolved topic. That
is the one thing nothing else here can do.

---

### Option E — adopt `aannoo/hcom` as-is

Zero build cost. Covers L0–L6 today.

**Choose if** the trust posture (L8) is genuinely out of scope — i.e. every agent and
every machine is fully trusted and prompt injection from a peer is an accepted risk.

**Note:** even if the answer is eventually "build something", hcom is worth running
first as a two-week probe. It will surface which layers actually matter in daily use far
faster than any further research.

---

## 6. Summary judgement

- **The transport layer is a commodity.** NATS, ejabberd and Ergo are all adequate; the
  choice between them turns on coordination primitives and topology, not messaging.
- **The real gap is L5 (delivery into a running session) and L8 (trust).** L5 has an
  official answer — Channels — that is *unavailable to anyone reaching the model through
  a cloud provider*, which is a legitimate reason to build. L8 has no answer anywhere, including in the closest prior art.
- **NATS leads on six of eight layers** and loses only L7, where the deficit is a
  missing UI over already-durable data.
- **ejabberd is the low-effort single-system alternative**, giving up leases and
  outbound-only topology in exchange for having one thing to operate instead of two.
- **Start with least-privilege grants regardless of the L8 decision.** Under NATS that
  is configuration, so the trust question can be deferred without incurring a rewrite.
