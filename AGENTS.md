# AGENTS.md (cost-tracker)

<!-- comm-contract:start -->

## Communication Contract

- Inherit global Codex communication and reporting rules from `/Users/d/.codex/AGENTS.override.md` and `/Users/d/.codex/policies/communication/BigPictureReportingV1.md`.
- Repo-specific instructions below add project constraints only; do not restate global voice or status-reporting rules here.
<!-- comm-contract:end -->

## Repo Rules

- This is a Python 3.12 package for live cost visibility; keep server/runtime behavior local and inspectable.
- Keep the MCP server entrypoint narrow: `cost-tracker` should continue to launch `cost_tracker.server:app` over stdio.
- Prefer focused parser and threshold tests for behavioral changes.

## Verification

```bash
uv run pytest
uv run ruff check .
```

<!-- portfolio-context:start -->
# Portfolio Context

## What This Project Is

cost-tracker is an active local project in the /Users/d/Projects portfolio.

## Current State

Portfolio truth currently marks this project as `recent` with `none` context. Phase 104 recovered minimum-viable context so future sessions can resume without rediscovery.

## Stack

- Primary stack: Python

## How To Run

- Review the README and top-level scripts before the next session; this repo does not yet expose one canonical run command inside the new context block.

## Known Risks

- This repo only has minimum-viable recovery context today; deeper handoff details may still live in the README and supporting docs.

## Next Recommended Move

Use this context plus the README and supporting docs to resume the next active task, then promote the repo beyond minimum-viable by capturing a dedicated handoff, roadmap, or discovery artifact.

<!-- portfolio-context:end -->
