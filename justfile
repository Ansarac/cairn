default:
    @just --list

# Install dependencies.
setup:
    uv sync

test:
    uv run pytest

lint:
    uv run ruff check .

fmt:
    uv run ruff format .

# Formatting as a gate rather than a fixer, because CI runs it as one.
#
# Not a nicety: `check` was `lint guard test`, CI is lint + `format --check` +
# guard + test, and the two drifted apart in the only direction that matters —
# `just check` was green on a tree CI would have rejected. Found this session,
# on `main`, two lines: hub.py's `_ack` and store.py's `Registration` return.
# Neither had anything to do with what was being worked on, and nobody had run
# `just fmt`, because nothing ever told them to.
fmt-check:
    uv run ruff format --check .

# The vendor-coupling guard: nothing outside adapters/ may mention a vendor.
#
# Two greps, not one, because "cursor" is also cairn's own word for the
# server-side read position — the `cursors` table, `last_acked_seq`, and every
# sqlite3 cursor object. Matching it case-insensitively would flag store.py's
# core vocabulary, so the editor is matched case-sensitively (`Cursor`) and in
# the spellings coupling would actually take (`cursor_cli`, `.cursor/`).
#
# The carve-out is narrower than it looks and one cut has already hit its edge:
# `\.cursor` matches **attribute access** as well as a dotted directory, so a
# field named `cursor` on any object trips this guard on every line that reads
# it. `wire.InboxPage.floor` is named for what it is partly for that reason, and
# the name turned out to be the better one anyway. If a future field genuinely
# wants to be called `cursor`, that is the conversation to have deliberately —
# widening the grep to buy it is what CLAUDE.md forbids.
guard:
    @! grep -rniE 'claude|anthropic|codex|copilot' src/cairn --include='*.py' \
        --exclude-dir=adapters \
    || (echo "vendor name leaked outside src/cairn/adapters/ — see CLAUDE.md 'The one structural rule'" && exit 1)
    @! grep -rnE 'Cursor|CURSOR|\.cursor|cursor[-_/](cli|agent|ide|rules)' src/cairn --include='*.py' \
        --exclude-dir=adapters \
    || (echo "vendor name leaked outside src/cairn/adapters/ — see CLAUDE.md 'The one structural rule'" && exit 1)
    @echo "guard ok: core is vendor-free"

# The whole gate, in CI's order. If this is green, CI is green.
check: lint fmt-check guard test

# Install cairn globally from this checkout.
#
# --reinstall is load-bearing, not belt-and-braces. With the version unchanged
# — which it is on every iteration between releases — `uv tool install --force .`
# serves the cached wheel and silently installs the code you had before your
# edit. Measured on uv 0.11.3, reproduced twice.
install:
    uv tool install --reinstall --force .

# Cut a release: tag main and push it. CI does the rest.
#
# What happens after this recipe returns, because none of it is guessable from
# here: the tag push runs `check`, then `image`, then a `release` job that builds
# the wheel and opens a **draft** release with both artifacts attached. It is not
# published and it is not `latest`. You publish it from the web UI, and that click
# is what makes it latest — GitHub refuses to put that flag on a draft, so there is
# no command here that could do it for you.
#
# The guards below are the same rules the release job enforces, run a second after
# you type this instead of four minutes into CI. They are not belt and braces: a
# tag is pushed before anything can check it, so a failure in CI leaves a bad tag
# on the remote that has to be deleted through admin bypass. Cheaper never to
# create it.
#
# Cut a release: `just release 0.2.0` tags main, pushes, and CI drafts the rest.
release version:
    #!/usr/bin/env bash
    set -euo pipefail
    [ "$(git branch --show-current)" = "main" ] || { echo "not on main"; exit 1; }
    [ -z "$(git status --porcelain)" ] || { echo "working tree is dirty"; exit 1; }
    git fetch --no-tags origin main
    [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "main is not in sync with origin"; exit 1; }
    declared=$(uv run python -c "import cairn; print(cairn.__version__)")
    [ "{{version}}" = "$declared" ] || { echo "asked for {{version}}, __version__ says $declared"; exit 1; }
    git tag -a "v{{version}}" -m "cairn {{version}}"
    git push origin "v{{version}}"
    echo "pushed v{{version}} — CI will draft the release; publish it from the web UI"

# Run the hub in the foreground, reachable from other machines, on a database
# that survives a reboot.
#
# 0.0.0.0 and a state-directory database are both deliberate: this recipe is the
# one people actually run, and a hub nobody else can reach — or one whose
# database /tmp reclaims — is a hub that has to be re-explained every time. The
# scratch equivalent is `just hub-dev`.
#
# With no CAIRN_TOKEN set this hub authenticates nobody, so binding it to a
# network means trusting everyone who can route to it. Set one on both ends to
# narrow that to everyone holding it — which is access control and not proof of
# who sent anything; peer mail still reads UNVERIFIED (docs/design.md §12 item 9).
# On a shared LAN, bind an interface too: `just hub 7777 10.0.0.5`.
hub port="7777" host="0.0.0.0":
    uv run cairn hub --port {{port}} --host {{host}} --db ~/.local/state/cairn/hub.db

# Run a throwaway hub for local experiments. Nothing here is meant to survive.
hub-dev port="7778":
    uv run cairn hub --port {{port}} --db /tmp/cairn-dev.db

# The same hub in a container, on a named volume that outlives it.
#
# --build every time on purpose: with the version unchanged between releases,
# which it is on every iteration, `up -d` alone happily starts the image you
# built before your edit — the same trap `just install` documents for wheels.
# docs/deployment.md has the network and database decisions this makes for you.
hub-up:
    docker compose up -d --build

# Stops the container and leaves the database. `down -v` takes the database too,
# which is why that one is not a recipe.
hub-down:
    docker compose down
