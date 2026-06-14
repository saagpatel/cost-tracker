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

Current portfolio truth should be checked in
`/Users/d/Projects/GithubRepoAuditor/output/portfolio-truth-latest.json`; recent
runs mark this project as `active-infra`. The repo still has minimum-viable
recovered context, so verify live branch, README, and package commands before
expanding scope.

## Stack

- Primary stack: Python

## How To Run

- Use `uv run cost-tracker` for the MCP server entrypoint. Run `uv run pytest`
  and `uv run ruff check .` before claiming verification.

## Known Risks

- This repo only has minimum-viable recovery context today; deeper handoff details
  may still need to be captured in a dedicated handoff or roadmap.

## Next Recommended Move

Use this context plus the README and supporting docs to resume the next active task, then promote the repo beyond minimum-viable by capturing a dedicated handoff, roadmap, or discovery artifact.

<!-- portfolio-context:end -->
