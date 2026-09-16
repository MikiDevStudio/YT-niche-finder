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
  There is no host venv, so run them in the image:
  `docker run --rm --network niche-finder_default -e POSTGRES_HOST=postgres -v "$PWD/backend:/app" niche-finder:latest python tests/test_smoke.py`
- Review `git diff` for unintended changes; mention affected MCP tools / endpoints in the PR.
- Work on a branch, open a PR against `main` of this fork, reference the issue.
- `gh` in this clone resolves to the upstream `pandich93/niche-finder`; pass
  `--repo MikiDevStudio/YT-niche-finder` to every `gh` call, or it reads the wrong issues and PRs.

## Local run on this machine

- `.env` is generated, not hand-edited: `.env.example` + `GOOGLE_API_KEY` from `~/.claude/.env`.
- Postgres host port is `5434` (5433 is taken by another container).
- Periodic use: `docker compose up -d postgres web worker`, then `docker compose stop`.
- `web` and `worker` mount `./backend` live and their HEALTHCHECK runs `db.init_db()` every 5 minutes, so a
  schema migration written on a branch reaches the real local database before it is even committed. Stop those
  containers while a migration must not run yet, and check what it did afterwards.
- A dashboard crawling on every screen has been Docker Desktop, not this code: when its engine answers
  `500 Internal Server Error` to `docker version`, restart Docker Desktop before profiling anything.
