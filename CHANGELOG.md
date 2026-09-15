# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Chrome extension** (`extension/`, Manifest V3) — vidIQ/NexLev-style
  panels on top of YouTube, served entirely from the local backend: outlier
  score against the channel's own median, view velocity and acceleration,
  views per subscriber, engagement, 30-day projection, revenue range and
  tags on a watch page; growth, grade, best publishing times, title patterns
  and similar channels on a channel page; multiplier badges on thumbnails in
  search, home and recommendations. Network access is limited to
  `127.0.0.1` by `host_permissions`.
- **`/api/inspect/video`, `/api/inspect/channel`, `/api/inspect/videos`** —
  endpoints behind the extension (`backend/application/inspection.py`).
  They read the local Postgres first and only fall back to a single
  `videos.list` / `channels.list` call (1 unit each, batched 50 ids per
  call) when a row is missing or stale — 6h for videos, 24h for channels —
  storing whatever they fetch, so browsing YouTube also fills the database.
- **`backend/tests/test_inspection.py`** — 13 tests for the above that need
  neither Postgres nor an API key (sqlite double for the store, stub for the
  HTTP client).
- **Tool annotations on all 43 MCP tools** — every tool now declares
  `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`
  as explicit booleans, so a client can tell a free local query from one that
  spends YouTube quota or deletes a row (and OpenAI's directory stops
  rejecting the server for missing hints). 28 tools are read-only;
  `destructiveHint` is true only for `untrack_channel` and
  `delete_saved_item`; `openWorldHint` is true for the 8 tools that reach the
  YouTube Data API. `collect_*` / `refresh_*` are marked non-idempotent
  because each call appends a new stats snapshot.

- **Tests for the remaining 17 MCP tools** — every tool is now exercised
  through `interfaces/mcp/server.py` itself rather than the layer beneath it
  (43/43, up from 26/43), with the YouTube client monkeypatched so nothing
  needs a key or the network. The new tests pin the behaviour the tool
  annotations claim: `refresh_stats` / `refresh_channels` append a second
  history row on a second call, `refresh_categories` upserts instead,
  `untrack_channel` keeps what was collected, and `calibrate_maturity_curve`
  writes nothing at all.

- **[PRIVACY.md](PRIVACY.md)** — what the project stores, where its traffic
  goes, and how to delete everything, linked from the README. Every claim is
  checked against the code: no telemetry, comments read by `video_comments`
  are never stored, and the application contacts exactly two external hosts
  (`www.googleapis.com` and `www.youtube.com`). It also records what a source
  scan misses — the embedding model is fetched from Hugging Face on first run
  — and what one gets wrong: `www.w3.org` is the Atom namespace identifier in
  `rss.py`, not a host anything connects to.

- **Rate limiting on the HTTP API** — a sliding one-minute window over
  `/api/*`, no new dependency. `RATE_LIMIT_PER_MINUTE` defaults to 600 and 0
  turns it off; over the limit the API answers 429 with `Retry-After`. The
  service listens on `127.0.0.1`, so this is a fuse against a looping client
  rather than a defence against outside traffic, and the default is sized from
  what the extension actually sends (~120-200 requests a minute while
  scrolling search results). Static frontend files are not counted, and the
  429 still carries CORS headers.
- **`preset="niche_all"` for `viral_videos_small_channels`** (MCP, `GET
  /api/viral`, `cli.py viral --preset`) — every channel of the selected niche,
  without the `max_subscribers` / `min_views` / `min_views_per_subscriber`
  filters, so big competitors show up next to small ones. Requires a niche;
  the dashboard offers it as «Каналы: все каналы ниши» once a niche is picked.
- **Second outlier baseline: `outlierScorePeriod`** — views against the median
  of the channel's long-form uploads within ±15 days of publication (uploads
  younger than 14 days skipped, channel median when the window has fewer than
  3). Returned next to `outlierScoreRolling` in sections, search, niche and
  channel analytics; `outlier_base="rolling"|"period"` (MCP tools, HTTP
  endpoints, `cli.py --outlier-base`, «База множителя» in the dashboard header)
  picks which one `outlierScore`, its band, sorting and `min_outlier_score`
  follow. Default stays `rolling`, so nothing changes unless asked.
- **Niche scatter: publication date × views** — `niche_videos` (MCP) and
  `GET /api/niches/{slug}/videos` return every video of a niche with its exact
  date, views, length, channel, both outlier scores, `isOutlier` (against
  `outlier_threshold`, default 2x) and `isFresh` (under 30 days). The niche page
  draws them on a log scale: the three largest channels in colour, the rest as
  «другие», outliers as large dots with the top five labelled, fresh videos
  hollow; channel and Shorts filters above the chart, outliers as a table below.
- **`outlier_base` in alerts, metadata review and the Chrome extension** —
  `scan_for_alerts` / `POST /api/events/scan?outlier_base=` and the worker's
  `WORKER_ALERTS_OUTLIER_BASE`; `review_metadata` / `POST /api/metadata/review`;
  `/api/inspect/video` and `/api/inspect/videos` (`outlierVsPeriod`,
  `outlierBasis`), with «База множителя» in the extension popup driving the
  video panel, the search badges, the channel panel and alert scans.

### Fixed

- **Viral videos «за 30 дней» no longer list years-old uploads.** The overview
  and the viral screen windowed by when a video entered the database, so a
  2021 video collected this week showed up as "viral in the last 30 days".
  Both now window by publication date; the viral screen still offers «по
  попаданию в базу» explicitly.
- **Test runners fail the process on a failed test.** `test_mcp_tools.py`,
  `test_http_rate_limit.py` and `test_http_api.py` printed FAIL but exited 0,
  so CI stayed green with broken tests. The same held for the `_run_all`
  runners of eight more files (#13), which CI did not run at all: CI now also
  runs `test_inspection`, `test_metadata_review`, `test_alerts`,
  `test_alerts_domain`, `test_library`, `test_rss`, `test_top_tags`,
  `test_keywords_domain` and `test_metadata_domain`, one process per file.
- **`collect_channel(..., niche=X)` now creates the niche row.** It used to
  write only `video_niches`, so `list_niches` and the dashboard's niche picker
  never showed a niche built from channels. The niche is slugified like
  `collect_niche` does, and `init_db` backfills rows for niches collected
  before the fix.

- **`scripts/mcp-docker.sh` больше не полагается на `docker run --env-file`.**
  `docker compose` читает `.env` по правилам dotenv и снимает кавычки вокруг
  значения, а `docker run --env-file` берёт строку буквально — из-за чего
  `YOUTUBE_API_KEY="AIza..."` попадал в контейнер вместе с кавычками. Ломался
  при этом только MCP-сервер (его запускает этот скрипт), а воркер и веб через
  compose работали как ни в чём не бывало: любой инструмент, ходящий в YouTube
  Data API, падал, а `db_stats`, `search_outliers` и эмбеддинги отвечали
  мгновенно. Скрипт теперь разбирает `.env` сам, снимает обрамляющие кавычки
  (одинарные и двойные), терпит CRLF, комментарии, пустые строки и префикс
  `export`, и передаёт переменные через `-e`.
- **`scripts/diag.sh`** — диагностика связки с YouTube API одной командой:
  `docker ps`, curl к `videoCategories`/`search` с хоста и изнутри контейнера,
  `cli.py doctor`, логи воркера. Пишет `scripts/diag-output.txt` с
  замаскированным ключом.

### Changed

- HTTP API now sends CORS headers for `chrome-extension://` origins; it
  still binds to `127.0.0.1` only.

## [0.1.0] - 2026-09-06

Initial public release.

### Added

- **MCP server** (`backend/interfaces/mcp/server.py`) — 24 tools for Claude
  Desktop: collection (`collect_niche`, `collect_channel`,
  `collect_trending`, `refresh_stats`, `refresh_channels`,
  `refresh_categories`), free sections (`viral_videos_small_channels`,
  `recently_added_outlier_channels`, `high_future_competition`,
  `most_popular_categories`, `trending_keywords`, `search_outliers`,
  `niche_overview`, `list_niches`, `db_stats`, `data_coverage`), and channel
  tracking/analysis (`track_channel`, `channel_analytics`,
  `compare_channels`, `channel_velocity`, `title_changes`,
  `best_time_to_publish`, `title_patterns`, `calibrate_maturity_curve`, and
  more).
- **HTTP API** (`backend/interfaces/http/api.py`, FastAPI) exposing the same
  use cases as the MCP server, for the dashboard.
- **Background worker** (`backend/interfaces/worker/main.py`) that snapshots
  view/subscriber counts on a schedule — the only reason growth rate,
  acceleration, and period comparisons can exist at all.
- **Dashboard** (`frontend/`, no build step, plain ES modules): Overview,
  Viral videos, Outlier channels, Categories, Keywords, Channel tracker,
  Niches, Channel detail, and Data screens.
- **PostgreSQL storage** with a schema migration path from the earlier
  SQLite-based prototype (`backend/migrate_sqlite_to_postgres.py`).
- **Docker Compose** setup bringing up Postgres, the worker, the dashboard,
  and the MCP server (stdio and HTTP profile) with one command.
- **CI** (GitHub Actions): smoke tests against a real Postgres service
  container, a `docker compose build` check, and an auto-updated test
  coverage badge.
- MIT license.

[Unreleased]: https://github.com/pandich93/niche-finder/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pandich93/niche-finder/releases/tag/v0.1.0
