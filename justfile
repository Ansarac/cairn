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

check: lint guard test

# Install cairn globally from this checkout.
install:
    uv tool install --force .

# Run a hub in the foreground against a scratch database.
hub port="7777":
    uv run cairn hub --port {{port}} --db /tmp/cairn-dev.db
