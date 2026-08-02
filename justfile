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

# Run the hub in the foreground, reachable from other machines, on a database
# that survives a reboot.
#
# 0.0.0.0 and a state-directory database are both deliberate: this recipe is the
# one people actually run, and a hub nobody else can reach — or one whose
# database /tmp reclaims — is a hub that has to be re-explained every time. The
# scratch equivalent is `just hub-dev`.
#
# cairn has no authentication and does not sign messages yet (docs/design.md §12
# item 7), so binding this to a network means trusting everyone who can route to
# it. On a shared LAN, bind an interface instead: `just hub 7777 10.0.0.5`.
hub port="7777" host="0.0.0.0":
    uv run cairn hub --port {{port}} --host {{host}} --db ~/.local/state/cairn/hub.db

# Run a throwaway hub for local experiments. Nothing here is meant to survive.
hub-dev port="7778":
    uv run cairn hub --port {{port}} --db /tmp/cairn-dev.db
