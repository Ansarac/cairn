# CLAUDE.md

Guidance for Claude Code when working in this repository.

**Sessions propose changes to this file; they do not make them.** A line here is
read as an instruction and obeyed, unlike a paragraph in `docs/design.md`, which
is read as an argument and can be disagreed with. Put the proposed wording in the
handoff and leave the decision to the maintainer. A hazard with a natural point of
use — a docstring, a test that fails — belongs there instead and needs nobody's
permission.

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

**Column zero belongs to cairn: fold every value from argv or the wire with
`render.oneline` before printing it — wherever it is printed**, `cli.py`'s own
confirmation lines included. `render.oneline`'s docstring argues the rule and
`docs/design.md` §12 item 5 has the forged inbox entry that produced it; what
neither can reach is a new `print()` somewhere in `cli.py`, which is why this
line exists.

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

The module docstrings carry the dependency direction. Two **absences** are rules
rather than description, and both have tests: `waiting` may not import `events`,
which is what keeps a bell frame undecoded and so keeps `kind` and
`correlation_id` out of the waiter's reach; and nothing imports `adapters` except
`cli`, through `adapters.default()` — `nudge` takes an **injected** state reader
instead, which is what keeps it vendor-free.

## Conventions

- Python 3.13, src layout, hatchling, `uv tool install`.
- ruff with `extend-select = ["ALL"]`, line length 120.
- **No third-party dependencies.** stdlib only, deliberately: cairn runs on a
  hardware bench where every package is one more thing that can break before a
  test run. Adding one needs a reason in `docs/design.md`.
- The skill ships inside the wheel. `locate_skill` has a source branch and a
  packaged branch and a user only ever exercises one — **test both**.

### Exit codes are an interface

`cli.py`'s module docstring lists them and says why `1` and `2` can never be
collapsed. One hazard is not visible there.

**A `WireError` reaching `run()` is a traceback plus exit `1`** — a stack trace
under the code for "asked, nothing to report", which is the poisoned-mailbox
shape wearing a different hat. It is a `ValueError`, so `run()` deliberately does
not catch it, and anything in `wire.py` that validates input must be converted at
the boundary instead: `cli._subject` → `UsageError`, `client._readable` →
`Unreachable`. This escaped three separate ways in one cut. Prove a new validator
with an exit-code assertion through `cli.run`, not a call to the helper.

## Hazards specific to this repo

**A change to `wire.py` without a `PROTOCOL_VERSION` bump is a silent break.**
Two builds will disagree and neither will say so, and when to bump and when not
to is on `PROTOCOL_VERSION` itself. Check with
`git diff <session-base>..HEAD -- src/cairn/wire.py` — **never the bare
`git diff`**, which compares the working tree with `HEAD` and so answers
"unchanged" for anything already committed.

**The bell is a minefield and all of it is written down at its points of use.**
`cli.cmd_bell` for the four properties it must keep, `wire.InboxPage` for why the
head cannot come from the page, `nudge.read_belled` for why there are two latches,
`nudge`'s counter constants for who may touch that file's mtime,
`adapters.claude_code.bell_payload` for the per-event envelope. Read them before
changing anything in that path; none of it is guessable from the call sites.

**Registration has three cases and the last two look identical on the wire** —
`store.register`'s docstring has all three and why `(machine, cwd)` is the
discriminator, and `tests/test_identity.py` pins them.

**A takeover must say what it stepped over.** `ack` moves forward only, so once a
takeover jumps the cursor to the head, the skipped mail is still in `messages` and
reachable by nothing. Registering therefore reports the case, the count, the
previous holder and a resume seq, and `cairn ack <seq> --rewind` is the one door
that moves a cursor backwards. Deleting either half turns a stated loss back into
a silent one, which is the thing `docs/design.md` §10 criticises other systems for.

**The stream is allowed to drop, and the nudger only ever types into `idle`.**
Both are counter-intuitive and both are argued at their points of use:
`events.py`'s module docstring on why making delivery reliable deadlocks the hub,
`client.STREAM_TIMEOUT` and `hub.HEARTBEAT_SECONDS` on why the two timeouts are
paired, `client.stream` on `read1` versus `read`, `nudge.WAKEABLE_STATES` and
`adapters.claude_code.session_state` on which states are safe to type into.

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

**And a live run driven by the session that wrote the cut tests the mechanism
but cannot test the reading.** Cut 5 shipped with three open questions for
exactly that reason and needed a second session to close them; two of the three
then came back *no*. Acceptance means an independent `claude -p` whose cwd is
**outside this repository**, so only the installed skill is in context, followed
by a blunt interview turn (`--continue`) asking what the surface did and did not
do. `docs/design.md` §12 item 5 records what that produced.

**Green tests are not the bar; a live run is.** Three things in that loop will
silently serve you stale code, and each has cost a session real time:

- `uv tool install --force .` **does not rebuild when the version is unchanged**;
  `just install` uses `--reinstall` for that reason and its comment has the rest.
- **A running hub ignores `store.py` and `hub.py` edits.** Restart it.
- `pkill -f "cairn hub --port 7801"` **kills the shell issuing it**, because the
  pattern matches its own command line.
  Break the pattern so it cannot match your own command line: `pgrep -af "port 777[8]"`.
  **The bracket only helps while the port appears nowhere else on that line.** It
  loses to `export CAIRN_HUB=http://127.0.0.1:7778 && pkill -f "port 777[8]"`, and
  to `pgrep -f "port 777"` itself. Four hits in one session, one of which ate a
  commit. Match nothing at all instead: `fuser -k 7778/tcp`.

```bash
just check      # lint + format check + the vendor guard + pytest — the whole CI gate
just hub        # :7777, every interface, ~/.local/state/cairn/hub.db — the real one
just hub-dev    # :7778 on loopback against /tmp/cairn-dev.db — throwaway
```

`just hub` binds `0.0.0.0` because a two-machine tool nobody else can reach is
not testable, and a hub with no `CAIRN_TOKEN` set authenticates nobody. Use
`just hub-dev` for scratch work, and read `docs/design.md` §11 item 3 before
putting the real one on a network you do not trust.

## Ending a session

Run `/handoff`. It is manual on purpose — never wire it to a hook.
