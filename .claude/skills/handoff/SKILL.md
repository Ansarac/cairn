---
name: handoff
description: >-
  Close out a work session on cairn: verify state from the repo rather than from
  memory, promote durable content into docs/design.md and the docstrings it belongs at,
  and rewrite the
  single handoffs/HANDOFF.md. Use when the user says they are done, asks to wrap up or
  write the handoff, or invokes /handoff.
---

# Close out a session

Invoked manually, at the end of a session. **Never wire this to a Stop hook** — the
user knows when a session ends and Claude does not, and a Stop hook fires every turn.
(cairn's own bell already uses that hook; a second thing there would fight it.)

The job is **triage and promotion**, not filling in a template. A template has fixed
sections, fixed sections get filled with something, and that is how a handoff grows
without bound. Each run is a chance to delete what has since found a home
elsewhere.

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
git log --oneline -15                    # find the commit this session started from
git diff --stat <base>..HEAD -- src/cairn/wire.py
```

`<base>` is the commit the session started from. The previous handoff usually names
it, and `git log --oneline -15` finds it otherwise. It is not optional — see the next
table.

Claims that need their own command, because memory is routinely stale on them:

| claim | how you are allowed to know it |
|:--|:--|
| the suite is green | run `just check`. The vendor guard trips on a string as easily as an import, so "I only added a comment" is not evidence |
| the protocol is unchanged | `git diff <base> -- src/cairn/wire.py` — **`<base>` with no `..HEAD`**, which covers committed and uncommitted work in one command. A shape change without a `PROTOCOL_VERSION` bump is a silent break between two builds, and *both* of the obvious spellings answer "unchanged" for a session that changed it, in opposite situations. Bare `git diff` compares the working tree against `HEAD`, so it goes empty the moment the change is committed — demonstrated 2026-08-02 against `hub.py`, bare form empty and `<base>..HEAD` 25 lines. `<base>..HEAD` compares two commits, so it goes empty while the change is still unstaged — demonstrated 2026-08-03 on the `described_at` fields, which were in the working tree and reported as no wire change at all. Same false clean, from the two commands you would reach for to avoid it |
| the skill still ships in the wheel | `uv build && python -c "import zipfile,glob;print([n for n in zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]).namelist() if '_skill' in n])"`. `force-include` fails silently |
| `cairn bell` is still safe | run it with the hub down. It must print `{}` and exit 0 |
| what a subagent changed | `git diff`, not its report — reports lag the final edits |

If a check fails, that is content. Say what failed and where it stops. Never report a
clean tree or a green suite you did not just observe.

## 2. Triage — three homes, and only one of them is transient

> **Why something is the way it is, and what it cost to find out** → `docs/design.md`
> **A hazard with somewhere to live** → the docstring at its point of use
> **This handover only** → `handoffs/HANDOFF.md`

### This skill does not edit `CLAUDE.md`

**Do not change `CLAUDE.md` as part of closing a session.** Not a line, not a
correction, not "while I was in there". If the work of the session genuinely produced
a rule that belongs there, **propose it in the handoff** — the words you would add and
where — and leave the decision to the maintainer.

The reason is that the repository is public. `CLAUDE.md` is the file that governs how
every future session behaves, and a contribution should not be able to rewrite the
instructions the next contributor works under as a side effect of landing something
else. Every other target here is inspectable as content: a paragraph in `docs/design.md`
is read as an argument and can be disagreed with. A line in `CLAUDE.md` is read as an
instruction and is obeyed. Those deserve different thresholds, and the second one is not
a threshold a session should clear on its own authority.

The bar is unchanged and worth keeping in front of you, because it is what makes a
*proposal* worth writing rather than noise: **likely, damaging, and invisible where the
mistake is made** — all three at once. Not "would a session do something wrong", which
everything passes.

Before it is even a candidate, three exits, in order:

1. **A point of use** — a docstring on the constant, function or test somebody must be
   editing anyway. Read exactly when relevant, free otherwise. Most "hazards" are this,
   and this is the answer that needs no permission from anybody.
2. **Reasoning** → `docs/design.md`, however painful it was to learn.
3. **This user, or how to run the loop** → memory.

**Carry the rule, not its evidence**, if you do end up proposing one. The bloat arrives
as a real one-line rule wearing the whole incident that produced it, when the incident
is already in `docs/design.md`. Imperative plus pointer. A paragraph that persuades
rather than instructs is in the wrong file.

`CLAUDE.md` is loaded in full into **every session, forever**, before anyone knows the
task, so a line that changes nobody's behaviour still costs attention on every unrelated
one. Cheap to add, paid for indefinitely. Across this project's history it has only ever
gone up, and the largest jumps came from the sessions most pleased with themselves.

| The item is… | it goes to |
|:--|:--|
| a hazard that is likely, damaging, **and** invisible at the point of use | a **proposal in the handoff**, quoting the line you would add. Never an edit |
| a hazard with a natural point of use | the docstring there |
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

> **Delete HANDOFF entirely — is anything *durable* lost?**
> It is gitignored, so a fresh clone runs this test for you whether you like it or not.
> Yes → promotion is underdone. Back to step 2. The test is about durable content
> being stranded, not about the file being dispensable: machine state and where the
> work stops are lost by design and belong nowhere else.
>
> **Does HANDOFF repeat anything `CLAUDE.md` or `docs/` already say?**
> Yes → it is overfilled. Cut it.

**Keep it under roughly 60 lines, and treat growth as a claim to be defended rather
than a defect on its own.** An earlier version of this
skill demanded the file get shorter every session. That is not a property a handoff
can have — its size is set by how much irreducible transient state the session leaves,
and a session that lands two workstreams and a new deployment path genuinely has more
to hand over than one that fixes a typo.

Worse, the metric bought its own failure. There are exactly two ways to hit it: drop
machine state, which is the non-recoverable content the file exists for; or push
transient items up into `docs/design.md`, where they read as durable reasoning and
nobody removes them. A rule whose cheapest satisfaction is the thing it was written to
stop is worse than no rule. If the file is over the ceiling, cut it and say what you
cut; if it grew and every line earns its place, say that instead of apologising for
arithmetic.

## 3b. If the session ran a live hub, archive its database

Most of `docs/design.md`'s reasoning comes from live runs, and a scratch hub lives in
`/tmp` — so the evidence evaporates while the paragraph stays, and a claim nobody can
re-read gets re-argued from zero. While the hub is still up:

```python
import sqlite3

src = sqlite3.connect("file:/tmp/<scratch>.db?mode=ro", uri=True)  # WAL-safe, stdlib
dst = sqlite3.connect("handoffs/archive/<cut>.db")
src.backup(dst)
```

**Not `cp`, and the difference is silent.** The hub opens its database with
`PRAGMA journal_mode=WAL`, so the rows live in the `-wal` sidecar until a checkpoint: a
copied `hub.db` is 4 KiB, `sqlite3.connect` succeeds on it, and the first query says
`no such table: messages`. Nothing fails at archive time. Reproduced while archiving
cut 5.

**Run the scratch hub in `/tmp` and archive into `handoffs/archive/` at the end.** Never
point a live hub at the archive path to save the copy. Doing that turns the archive step
into snapshot-then-replace, and the replaced file's `-wal` and `-shm` sidecars stay on
disk beside a database they no longer describe — which SQLite will try to replay. Caught
by re-counting rows after the move; silent otherwise.

Render the companion `.md` **through the commands a reader would actually run** rather
than as a table dump, so the I1 framing tiers survive. `handoffs/` is gitignored: the
copy stays local, and the handoff points at it.

## 4. Commit, then emit the paste prompt

`CLAUDE.md` and `docs/` are tracked; the handoff is not. Promotion is the only way
content reaches anyone else — that is the point. Commit and push on the session branch,
never `main`, unless the user says otherwise.

Then one line for the next session — **a pointer, not a payload**:

```
Continue cairn. Read handoffs/HANDOFF.md and the docs/design.md § it points at,
then: <one concrete action>
```

## What this repo needs said in the handoff itself

`CLAUDE.md` already carries the rules; repeating them here would be the same bloat this
skill exists to prevent. These are about **reporting**, which is a different act.

- **Re-run `just check` yourself after subagent edits.** Their "all checks passed"
  routinely predates their last write.
- **If `tests/test_walking_skeleton.py` is red, that is the first sentence**, and nothing
  else may be described as working.
- **If the session weakened an invariant test — the absence assertion in
  `tests/test_wire.py`, or anything in `tests/test_render.py` — say so in bold.** They
  look cosmetic. They are the two invariants in executable form.
- **If the session drifted toward starting, resuming or driving a session, say so
  plainly.** That is out of scope by design, and it is the failure mode that killed the
  closest comparable project — a drift nobody names is a drift that continues.
- **Name every file you changed that changes future behaviour**: `skills/cairn/SKILL.md`
  and this file. Both are easy to leave unmentioned and both outlive the session.
- **If you are proposing a `CLAUDE.md` line, say so under its own heading** with the
  words you would add. It is a request, not a change, and burying it in prose is how it
  gets read as one — or missed entirely.
- Cite `file:line` for anything a reader will need to act on.
