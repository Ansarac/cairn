# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`cairn` lets coding agent sessions that a human **already started**, on different
machines, register a name and message each other. That is the entire product.

It does not start, resume, supervise or kill sessions. It does not wrap an agent
CLI. It does not know which agent product is on either end.

If a task appears to need any of those, it is out of scope **by design**, not by
omission — say so rather than building it. `docs/design.md` §1 has the reasoning,
including the two independent forces behind it: session lifecycle is the one
thing that couples a tool to a vendor's process model, and the most popular
project in this space deprecated itself precisely because of that coupling.

## The three invariants

Check every change against these. `docs/design.md` §3 carries the measurements
behind each; do not restate them here.

**I1. Peer content must arrive through a named, installed tool, and its
provenance must be verified rather than asserted.** Content reaches a model
because the agent ran `cairn inbox`, never because a hook pasted it in. And no
message may carry a field claiming its own trustworthiness — that is why
`Message` has no `verified`, and why `Provenance` is built only by the code that
ran a check. There is a test asserting that absence. If you find yourself
deleting it, stop.

The framing is **tiered**, and the tiers are load-bearing: the provenance verdict
rides every message, its explanation is said once per reading, and the reasoning
lives only in `SKILL.md`. Moving the verdict into the footnote, or the
explanation back onto every line, each have a test. `docs/design.md` §3 carries
the measurement — including why the middle tier survived and why `--json` grew.

A verdict is also worth only what it *distinguishes*. `UNVERIFIED` went wallpaper
on a live run for being identical everywhere with nothing to differ from, and cut
5's answer was not a second tier but a different **subject**: on the inbox it
qualifies who sent this, on `cairn sent` whether this is your record at all. If
you add a surface, ask what its verdict is about before reusing a clause.

**And column zero belongs to the renderer, on every string, not just bodies.**
Anything from argv or off the wire is folded with `render.oneline` before it is
printed — *wherever* it is printed, `cli.py`'s own confirmation lines included.
Bodies were safe by accident, because they go through `splitlines()`; a
correlation id, an artifact host, or any name went into an f-string whole, and
one `--correlation` containing a newline forged a `verified(ed25519)` entry from
`operator`. Nothing validates a name anywhere — `normalize_subject` is why
subjects alone need no fold. Fixing it in `wire.py` instead is a
`PROTOCOL_VERSION` question, so do not. The parametrised tests list every field
so a new one has to join them; the fix landed in the renderers first and `cli.py`
went uncovered for an hour, which is the whole lesson.

**I2. The receiver controls attention.** A sender may ring a bell. A sender
never decides when the receiver reads. The bell carries a count and never
carries content.

A `note` is the limit case: no recipient, so it rings nothing and reading it
consumes nothing. Both absences are tested. A bell on notes, or a per-agent read
position, turns a pile into a queue.

**I3. cairn declares intent; it does not enforce it.** A claim says someone is
using a resource. It is not a lock. Real exclusion over hardware belongs to the
kernel on the machine that owns it.

## The one structural rule

**Nothing outside `src/cairn/adapters/` may name a vendor** — not in an import,
not in a path, not in a string. `just guard` greps for it and CI fails on it.

This is not tidiness. cairn's claim is that it works with any agent that can run
a shell command, and that claim survives years only if there is exactly one
place where it could go wrong. When the guard goes red, the fix is to move the
knowledge into an adapter, never to widen the grep.

## Layout

```
src/cairn/
  wire.py        the contract: message schema + PROTOCOL_VERSION. Imports nothing local.
  errors.py      exceptions carrying their exit code
  store.py       Store protocol + SqliteStore. Server-side cursors live here.
                 Notes live here too, and they have no cursor — see I2.
  events.py      SSE codec + in-process fan-out. Allowed to drop; read its docstring.
  hub.py         stdlib HTTP + the SSE route. Parse, call one store method, serialize.
  client.py      the only module that knows the hub is reachable over HTTP
  waiting.py     when `cairn inbox --wait` may stop. Imports client; never events.
  terminal.py    tmux pane discovery and safe one-line injection. Imports nothing local.
  nudge.py       the optional daemon: local counter, two latches, wake decision
  cli.py         argument parsing, dispatch, exit codes. No rules.
  render.py      output — the framing, which tier it sits in, and `oneline`
  provenance.py  what this build actually verified. Currently: nothing, loudly.
  config.py      hub URL (config) and per-directory identity (state)
  skill.py       dual-branch skill lookup: source checkout and packaged wheel
  adapters/
    __init__.py      default() — the only way core reaches a product
    claude_code.py   skills dir, turn-boundary hooks, session state
skills/cairn/SKILL.md    force-included into the wheel
docs/design.md           why everything is the way it is
```

Dependency direction, which the module docstrings also state: `cli → client →
wire`, `cli → waiting → client`, and `hub → store → wire`. `waiting` may not
import `events` — that absence is what keeps a bell frame undecoded and so keeps
`kind` and `correlation_id` out of the waiter's reach, and there is a test for
it. `nudge` depends on `client`, `events`, `terminal`
and an **injected** state reader — never on `adapters`, which is exactly what
keeps it vendor-free. Nothing imports `adapters` except `cli`, and only through
`adapters.default()`.

## Conventions

- Python 3.13, src layout, hatchling, `uv tool install`.
- ruff with `extend-select = ["ALL"]`, line length 120.
- **No third-party dependencies.** stdlib only, deliberately: cairn runs on a
  hardware bench where every package is one more thing that can break before a
  test run. Adding one needs a reason in `docs/design.md`.
- `run()` converts `CairnError` to an exit code and lets real bugs keep their
  traceback. Do not widen that catch.
- The skill ships inside the wheel via
  `[tool.hatch.build.targets.wheel.force-include]`. `locate_skill` has a source
  branch and a packaged branch — **both are hot paths**, test both.

### Exit codes are an interface

`0` fine · `1` asked, nothing to report · `2` hub unreachable · `3` cannot be
carried out as asked · `130` interrupted.

`1` and `2` mean opposite things and must never be collapsed. An empty inbox is
an answer; an unreachable hub means messages are not being delivered and nobody
is being told.

**A `WireError` reaching `run()` is a traceback plus exit `1`** — a stack trace
under the code for "asked, nothing to report", which is the poisoned-mailbox
shape wearing a different hat. It is a `ValueError`, so `run()` deliberately does
not catch it, and anything in `wire.py` that validates input must be converted at
the boundary instead: `cli._subject` → `UsageError`, `client._readable` →
`Unreachable`. This escaped three separate ways in one cut. Prove a new validator
with an exit-code assertion through `cli.run`, not a call to the helper.

A malformed command line is `3`, and that costs one non-obvious line of code:
argparse's own `error()` exits **2**, so `cli._Parser` overrides it to raise
`UsageError` and `run()` parses inside its `try`. Subparsers inherit the class
from the root parser, which is the only reason every subcommand gets this — pass
`parser_class=` to `add_subparsers` and the guarantee is gone with no test to
notice. A session found the old behaviour by mistyping a flag and wondering
whether the hub had died.

## Hazards specific to this repo

**A change to `wire.py` without a `PROTOCOL_VERSION` bump is a silent break.**
Two builds will disagree and neither will say so. Run `git diff -- src/cairn/wire.py`
before finishing any session that touched it.

**And a bump for a purely additive change is a loud one.** `check_version`
compares for equality, so a bump does not deprecate an old peer, it disconnects
one — on every route, including the unchanged ones. Bump when an existing shape
changes meaning, never when a new one appears. Worked example and full reasoning
on `PROTOCOL_VERSION` itself.

**`cairn bell` must never fail loudly.** It runs from another program's hook, so
an exception there degrades the session it is attached to. Every failure path
prints `{}` and exits 0. If you touch it, verify with the hub down.

**The bell must not ring twice for the same mail.** It latches on the highest
seq it has rung for. Without that, a reader who chose not to open the inbox gets
a loop instead of a reminder.

**And it currently goes deaf past `--limit`, which is open.** That highest seq is
read off the same capped window the inbox returns, so once the unread count
exceeds the limit the head stops moving, the latch pins to it, and the
turn-boundary bell is **permanently silent** until the reader drains by hand.
`nudge`'s counter is built the same way. Known, not fixed, and the fix is the hub
returning the true `MAX(seq)` on the inbox response — which is a `wire.py` change
and so a `PROTOCOL_VERSION` question. Do not "simplify" the latch without reading
`docs/design.md`'s appendix row on it first.

**Registration has three cases, and the last two look identical on the wire.** A
new name parks the cursor at the current head, so a fresh session is not buried
under a month of other people's mail. A returning session — same name, same
`(machine, cwd)` — keeps its cursor, so a restart still gets its backlog. A
**takeover** — the same name from anywhere else — parks at the head too, because
otherwise a stranger inherits the conversation, which was reproduced against a
live hub before it was fixed. `(machine, cwd)` is the discriminator; `session_id`
is stored but is `None` whenever the host product exports none, so it cannot be
the test. Getting any of the three wrong is immediately visible to users and none
is obvious from the code — `tests/test_identity.py` pins all three.

**A takeover must say what it stepped over.** `ack` moves forward only, so once a
takeover jumps the cursor to the head, the skipped mail is still in `messages` and
reachable by nothing. Registering therefore reports the case, the count, the
previous holder and a resume seq, and `cairn ack <seq> --rewind` is the one door
that moves a cursor backwards. Deleting either half turns a stated loss back into
a silent one, which is the thing `docs/design.md` §10 criticises other systems for.

**The sending side pins names too, and it fails closed.** `config.check_pin`
records what a name reached the first time this directory sent to it and raises
`NameMoved` if that changes; `cairn forget <name>` is the escape hatch. This is a
declaration, not enforcement (I3) — the hub still cannot know which claimant is
legitimate. It exists so the failure is *loud on the sender* rather than silent
on both ends.

**The bell stream is allowed to drop; making it reliable breaks the hub.** If a
subscriber's queue fills, `events.py` discards and counts rather than waiting —
because `publish` runs on the thread part-way through storing somebody else's
message, so a blocking queue lets one wedged reader stall delivery for everyone.
This is safe only because every bell triggers a full authoritative `inbox` fetch
and the payload is discarded. Do not "fix" the dropping.

**Both ends of a stream need a periodic write, and the timeouts are paired.** The
hub heartbeats so it notices a departed reader — with no write, the handler blocks
forever and subscriptions accumulate. The client's socket timeout notices a
departed hub. Set the client timeout below `hub.HEARTBEAT_SECONDS` and every quiet
stream tears itself down on a timer.

**Use `read1`, never `read`, on a streaming body.** `HTTPResponse.read(n)` blocks
until it has all `n` bytes or the connection closes, so a sixty-byte bell sits
unseen behind a 4 KiB buffer. This was measured, and the obvious code is wrong.

**Only ever type into a session reported `idle`.** `busy` fights the input buffer;
`waiting` means the session is on a prompt, so the nudge becomes the answer to it;
an unrecognised status is not a safe status. A record whose pid is dead is not
`idle` either — those files outlive the process that wrote them.

**Two latches, not one.** Typing into a terminal and speaking at a turn boundary
are separate channels reaching the same reader. Sharing a latch means a nudge
silences the next turn-boundary bell, so the reader is woken and then told nothing.

**Only the daemon may advance the counter file's mtime.** That mtime *is* the
daemon-liveness signal `counter_is_fresh()` reads, and the counter file has two
writers: the daemon, and `cairn bell` latching what it just announced. When the
latch touched the mtime, a hook on a machine with no nudger forged a heartbeat
for a daemon that did not exist and then believed the empty record it had just
written — so the bell went deaf for 90 seconds after every single ring. Hence
`_write_record(..., keep_mtime=True)` on both latch paths. Any new writer to that
file has to decide the same question, and the answer is almost always "keep it".

**Do not re-litigate the eliminated options.** `docs/design.md` records, with
measurements, why these were rejected: a message bus (§7), A2A or MCP on the
wire (§11), building on Happy (§8), bridging the built-in agent-teams mailbox
(§4), and MCP as the agent-facing surface (§6). Each was a serious candidate. If
you want to reopen one, read the section first, and add counter-evidence *there*
rather than arguing it fresh in a handoff.

## Writing the docs

This is a public repository, and the prose is deliberately specific: nearly every
claim is attached to a measurement or a failure. Keep it that way — an abstracted
rationale ("shared resources can end up inconsistent") persuades nobody, while the
concrete one it came from ("a crashed flash leaves a board half-written, so the
next claimant has to be told where the last one stopped") does.

**An example must not be liftable as an answer.** In a tool whose output is
durable, an example that answers a question plausibly enough to transplant will
be transplanted — a live session began writing `SKILL.md`'s invented root cause
into permanent sediment as its own finding, and nearly adopted an example subject
as a convention. Weld an example answer to its own particulars. `docs/design.md`
§12 item 4 has the incident.

The rule when adding to it: **keep the shape of the problem, drop its identity.**
Role words — bench, compute, infra, rig, firmware, `hil`, `jtag` — are industry
vocabulary and stay. What must not appear: a specific product domain, an internal
hostname, a port, a service running on someone's network, or a path under a real
home directory. Examples in prose, tests and `SKILL.md` all count.

## Testing

`tests/test_walking_skeleton.py` is the one that matters: a real hub on a real
socket, two agents, a message crossing between them, and a real SSE bell stream.
The risks in this project live between modules, not inside them — that is why the
end-to-end test was written first and should stay the first one read. If it goes
red, nothing else in the suite matters. Both bugs found while wiring the bell
stream (`read` vs `read1`, and the inherited timeout) were invisible to every
unit test and only appeared here.

Every test is offline. The hub binds an ephemeral loopback port, and no test
spawns a process, drives real tmux, or reads real `/proc`.

**Green tests are not the bar; a live run is.** Three things in that loop will
silently serve you stale code, and each has cost a session real time:

- `uv tool install --force .` **does not rebuild when the version is unchanged**
  — it reports success and keeps the old wheel. Build first:
  `uv build --wheel -o /tmp/w && uv tool install --force /tmp/w/cairn-*.whl`.
- **A running hub ignores `store.py` and `hub.py` edits.** Restart it.
- `pkill -f "cairn hub --port 7801"` **kills the shell issuing it**, because the
  pattern matches its own command line.

```bash
just check      # lint + format check + the vendor guard + pytest — the whole CI gate
just hub        # :7777, every interface, ~/.local/state/cairn/hub.db — the real one
just hub-dev    # :7778 on loopback against /tmp/cairn-dev.db — throwaway
```

`just hub` binds `0.0.0.0` because a two-machine tool nobody else can reach is
not testable, and cairn does not authenticate. Use `just hub-dev` for scratch
work, and read `docs/design.md` §11 item 3 before putting the real one on a
network you do not trust.

## Ending a session

Run `/handoff`. It is manual on purpose — never wire it to a hook.
