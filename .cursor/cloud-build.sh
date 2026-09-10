#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Keep the Cloud Agent build on the repository's declared Python and lockfile.
uv python install 3.12
uv sync --all-groups --frozen

# The build is intentionally self-contained: no project MCPs, services, secrets,
# ccusage binary, bridge database, or live transcript data are needed. Type-check
# the deterministic core modules because the optional runtime wrappers depend on
# external command/database state and are covered by fixture tests below.
uv run ruff format --check .
uv run ruff check .
uv run ty check \
  src/cost_tracker/bridge_db.py \
  src/cost_tracker/classify.py \
  src/cost_tracker/session_sync.py \
  src/cost_tracker/thresholds.py \
  src/cost_tracker/transcripts.py
uv run pytest -q
