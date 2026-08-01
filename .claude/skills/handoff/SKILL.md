---
name: handoff
description: >-
  Close out a work session on cairn: verify state from the repo rather than from
  memory, promote durable content into CLAUDE.md and docs/design.md, and rewrite the
  single handoffs/HANDOFF.md. Use when the user says they are done, asks to wrap up or
  write the handoff, or invokes /handoff.
---

# Close out a session

Invoked manually, at the end of a session. **Never wire this to a Stop hook** — the
user knows when a session ends and Claude does not, and a Stop hook fires every turn.
(cairn's own bell already uses that hook; a second thing there would fight it.)

The job is **triage and promotion**, not filling in a template. A template has fixed
sections, fixed sections get filled with something, and that is how a handoff grows
without bound. Each run is a chance to make the file smaller.

**One file: `handoffs/HANDOFF.md`.** Overwritten every time, never appended,
**gitignored**. That last part is load-bearing rather than incidental: anything left
only in the handoff does not survive a fresh clone and reaches nobody. A durable item
still sitting there at the end of a run is a defect, not a filing choice.

## 1. Get the facts from the repo, never from memory

**This is the step that earns the skill its keep.** The failure mode is not a
forgotten handoff, it is a confidently wrong one. A missing handoff is obvious. A
wrong one is trusted.

```bash
git status --short --branch
git rev-list --count @{u}..HEAD          # unpushed, the actual number
just check                               # ruff + vendor guard + pytest
git diff --stat -- src/cairn/wire.py     # see the next table
```

Claims that need their own command, because memory is routinely stale on them:

| claim | how you are allowed to know it |
|:--|:--|
| the suite is green | run `just check`. The vendor guard trips on a string as easily as an import, so "I only added a comment" is not evidence |
| the protocol is unchanged | `git diff -- src/cairn/wire.py`. A shape change without a `PROTOCOL_VERSION` bump is a silent break between two builds |
| the skill still ships in the wheel | `uv build && python -c "import zipfile,glob;print([n for n in zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]).namelist() if '_skill' in n])"`. `force-include` fails silently |
| `cairn bell` is still safe | run it with the hub down. It must print `{}` and exit 0 |
| what a subagent changed | `git diff`, not its report — reports lag the final edits |

If a check fails, that is content. Say what failed and where it stops. Never report a
clean tree or a green suite you did not just observe.

## 2. Triage — three homes, and only one of them is transient

> **A rule that changes what a session does** → `CLAUDE.md`
> **Why something is the way it is, and what it cost to find out** → `docs/design.md`
> **This handover only** → `handoffs/HANDOFF.md`

`CLAUDE.md` carries rules and hazards. It must carry **no state**: no current status,
no dates, no in-progress work. *If you are about to write a date into `CLAUDE.md`, the
content belongs somewhere else.* The test is narrow: **would a session that has not
read this do something wrong or expensive?** "The bell must never fail loudly"
qualifies. "We are mid-way through cut 2" does not.

| The item is… | it goes to |
|:--|:--|
| a rule or hazard that prevents damage, waste, or a silent break | **`CLAUDE.md`** — a line or a row, then link out |
| an architectural decision, or an option considered and **rejected** | `docs/design.md`, in the relevant §, **with the reasoning** |
| a measurement | the measurements table in the `docs/design.md` appendix |
| a change to what cairn is or is not | `README.md` "What it is not", and `docs/design.md` §1 |
| a change to the agent-facing contract | `skills/cairn/SKILL.md` — and say so, it changes peer behaviour |
| how to work with *this user*, or a cross-session gotcha | memory |
| half-done work, a live blocker, a next step | stays in the handoff |

**Record eliminated options with their reasoning.** This matters here more than in most
repos: NATS, A2A, MCP-as-the-surface, building on Happy, and bridging the built-in
agent-teams mailbox were each a serious candidate rejected on evidence. An unexplained
rejection gets re-proposed and re-argued from zero by the next fresh context, which is
the most expensive thing that happens at a session start. If this session eliminated
something, the reason goes in `docs/design.md` before the handoff is written.

Two habits worth the keystrokes:

- **Record the evidence, not the verdict.** "`kv.Update()` silently clears the TTL and
  returns no error, so the lease becomes permanent" survives re-reading. "NATS leases
  are risky" does not.
- **Correct stale items in place, with the measurement**, so a reader can tell "checked
  and false" from "never looked at".

Anything promoted **leaves the handoff.** A pointer may stay if the next session must be
aimed at it; the content does not live in both places. Write the promotions *before*
rewriting the handoff, so step 3 is deleting text that already has a home.

## 3. Rewrite `handoffs/HANDOFF.md`

Overwrite completely. It is one file, not a log. Include only what cannot be recovered
from the repo:

- what is half-done, and **where exactly it stops**
- what is blocked, and **on what specifically** — a person, a measurement, a decision?
- decisions taken in conversation that no file records
- the next concrete step
- which cut of `docs/design.md` §12 the work is in, if that is not obvious

**Anti-bloat rules, applied literally:**

- Omit empty sections. Do not write "Nothing blocked" under a Blocked heading.
- **Never restate `git log`.** Branch and divergence, not a commit list.
- Delete anything `CLAUDE.md` or `docs/design.md` already says. Point at it.
- Drop anything already expired.

Then the boundary test, both directions:

> **Delete HANDOFF entirely — can the work resume from `CLAUDE.md` + `docs/` + the repo?**
> It is gitignored, so a fresh clone runs this test for you whether you like it or not.
> No → promotion is underdone. Back to step 2.
>
> **Does HANDOFF repeat anything those already say?**
> Yes → it is overfilled. Cut it.

**The measurable signal is that this file gets shorter over time.** If it grew,
promotion did not happen — say so out loud rather than shipping the growth.

## 4. Commit, then emit the paste prompt

`CLAUDE.md` and `docs/` are tracked; the handoff is not. Promotion is the only way
content reaches anyone else — that is the point. Commit and push on the session branch,
never `main`, unless the user says otherwise.

Then one line for the next session — **a pointer, not a payload**:

```
Continue cairn. Read handoffs/HANDOFF.md and the docs/design.md § it points at,
then: <one concrete action>
```

## Reminders specific to this repo

- **`wire.py` is the contract.** A changed shape needs `PROTOCOL_VERSION` bumped in the
  same commit. Nothing will tell you otherwise until two machines disagree in the field.
- **The vendor guard is easy to trip and easy to defeat.** If `just guard` went red, move
  the knowledge into `src/cairn/adapters/`. Widening the grep in the justfile or CI is
  never the fix, and doing it quietly hollows out the project's main claim.
- **The invariant tests are not ordinary tests.**
  `tests/test_wire.py::test_a_sender_cannot_claim_its_own_message_is_verified` asserts an
  absence, and everything in `tests/test_render.py` asserts framing. Both look cosmetic
  and are not. If a session weakened either, that belongs in the handoff in bold.
- **`tests/test_walking_skeleton.py` is the load-bearing test.** If it is red, report that
  first and do not describe anything else as working.
- **Re-run `just check` yourself after subagent edits.** Their "all checks passed"
  routinely predates their last write.
- **Scope creep here has one specific shape**: something that needs cairn to start,
  resume or drive a session. That is excluded by design (`README.md` "What it is not").
  If a session drifted that way, say so plainly — it is the failure mode that killed the
  closest comparable project.
- Cite `file:line` for anything a reader will need to act on.
- If the session touched `CLAUDE.md`, `skills/cairn/SKILL.md`, or this file, say so —
  those change how future sessions and peer agents behave, and are easy to leave
  unmentioned.
