# niche-finder (MikiDevStudio fork)

Fork of [pandich93/niche-finder](https://github.com/pandich93/niche-finder), used as the data and dashboard layer for
[MikiDevStudio/YT-analyze](https://github.com/MikiDevStudio/YT-analyze) (niche research, competitor breakdowns, video ideas).
Feature work is tracked as issues in this fork.

## Working on the code

- Understand before editing: read `backend/README.md` (layers, formulas, quotas) and the callers of what you change
  (`grep`, or `graphify` for a structural overview). GitNexus is not used here: its PolyForm Noncommercial license
  does not fit this project.
- Keep the layering: pure rules in `backend/domain/`, adapters in `backend/infrastructure/`, use cases in
  `backend/application/`, thin `interfaces/` (MCP, HTTP, CLI, worker). MCP and HTTP must call the same use case,
  never two implementations.
- New MCP tool or HTTP endpoint: add a test next to the existing ones in `backend/tests/`.
- Frontend stays build-free (plain ES modules, no npm, no CDN). Charts go through the components in `frontend/ui.js`.
- Code from GPL projects (e.g. YouTubeLedger) is reference only — reimplement, never copy, the fork is MIT.

## Before committing

- Run the tests the way CI does, one process per file (`.github/workflows/ci.yml`); `make up-db` first.
- Review `git diff` for unintended changes; mention affected MCP tools / endpoints in the PR.
- Work on a branch, open a PR against `main` of this fork, reference the issue.

## Local run on this machine

- `.env` is generated, not hand-edited: `.env.example` + `GOOGLE_API_KEY` from `~/.claude/.env`.
- Postgres host port is `5434` (5433 is taken by another container).
- Periodic use: `docker compose up -d postgres web worker`, then `docker compose stop`.
