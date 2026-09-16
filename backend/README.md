# niche-finder — backend

[![CI](https://img.shields.io/github/actions/workflow/status/pandich93/niche-finder/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/pandich93/niche-finder/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fpandich93%2Fniche-finder%2Fmain%2Fassets%2Fcoverage.json&query=%24.totals.percent_covered_display&suffix=%25&label=coverage&style=flat-square)](../assets/coverage.json)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue?style=flat-square&logo=python&logoColor=white)](Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)](interfaces/http/api.py)
[![PostgreSQL 16](https://img.shields.io/badge/postgres-16-336791?style=flat-square&logo=postgresql&logoColor=white)](../docker-compose.yml)
[![MCP](https://img.shields.io/badge/MCP-49%20tools-8A2BE2?style=flat-square)](interfaces/mcp/server.py)

A self-hosted alternative to NexLev / vidIQ / ViewStats: find niches, viral
videos from small channels, trending categories and keywords **over
arbitrary periods** (24h, 48h, 7/30/90 days), plus full channel tracking and
analytics. All on the free YouTube Data API v3 and local PostgreSQL.

Classification like "faceless / AI / on topic" isn't done by the server —
it's done by the model calling these tools: the server returns raw titles,
descriptions, and thumbnails, and the decision gets made in the
conversation. No separate paid LLM key is needed.

Market research and competitor formulas (`docs/research-tools.md`) are
internal notes, not included in this repository.

---

## Quota essentials (changed June 1, 2026)

| Method | Cost |
|---|---|
| `search.list` | 1 unit, but **only 100 calls a day**, a separate bucket |
| everything else | 1 unit out of the shared pool of **10,000 units a day** |

Search is scarce, reading is nearly free. So:

- `collect_niche` — the only one that spends search. Use it for new topics.
- `collect_channel` — goes through the uploads playlist: **1 unit per 50
  videos**, no 500-result cap, doesn't touch search. The main way to build
  up the corpus.
- `refresh_stats` — via `videos.batchGetStats`, ~1 unit per 50 videos.

Every collector returns a `quota` field with the actual spend.

Also: since July 21, 2025 `chart=mostPopular` only returns Music, Movies, and
Gaming charts — YouTube no longer has a general Trending tab. So
`most_popular_categories` and `trending_keywords` are computed from your own
corpus, not from the chart.

---

## Running with Docker (recommended)

```bash
cd ~/Desktop/projects/youtube/analytic
cp .env.example .env          # fill in YOUTUBE_API_KEY — it builds without
                              # one, but there'll be nothing to collect with
docker compose build
docker compose up -d web worker   # also brings up postgres (depends_on)
open http://localhost:8080        # or: make open
docker compose logs -f worker
```

Upgrading from the old SQLite version and want to keep your collected data?
`python3 backend/migrate_sqlite_to_postgres.py path/to/old/niches.db` (once,
after `docker compose up -d postgres`; safe to run again).

The dashboard is `frontend/`, see [frontend/README.md](../frontend/README.md).
It shows the same sections as the MCP tools, and only listens on localhost.

The key is only needed at runtime, not at build time: `docker compose build`
works fine with an empty `.env`. If there's no key, the worker says so and
exits, while the read tools keep working off whatever's already collected.

The worker isn't optional, it's a requirement: the YouTube API only ever
returns "how many views right now". View-gain speed, acceleration,
subscriber growth, period comparisons, and thumbnail/title-change detection
only exist because something is regularly recording the numbers. That's the
worker's job.

Claude Desktop connection — in
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "niche-finder": {
      "command": "/usr/local/bin/docker",
      "args": ["run", "--rm", "-i",
               "--network", "niche-finder_default",
               "--env-file", "/path/to/niche-finder/.env",
               "-e", "POSTGRES_HOST=postgres",
               "-v", "niche-finder-models:/models",
               "niche-finder:latest", "python", "server.py"]
    }
  }
}
```

Use the absolute path to `docker` (`which docker`), not just `"docker"` —
Claude Desktop's MCP launcher doesn't always inherit your shell's `PATH`.

There's also a `scripts/mcp-docker.sh` launcher that fills in `--env-file`
and the volumes for you, so you can point `command` at it directly instead
of writing out the full `docker run` line. **On macOS it can fail with
`Operation not permitted` / `Server disconnected`**, even though the same
script runs fine from a terminal — Claude Desktop's MCP process appears to
be sandboxed and unable to exec an arbitrary script it didn't create,
regardless of the script's file permissions. If you hit that, use the raw
`docker` command above instead (it invokes the already-trusted `docker`
binary directly, so the sandbox restriction doesn't apply).

### If the build fails at Docker Hub

```
failed to fetch anonymous token: ... lookup auth.docker.io: i/o timeout
```

This is a network issue, not a code issue: Docker can't reach the image
registry. The usual culprit is an active VPN (corporate clients like
AnyConnect regularly tunnel or drop traffic to the registry) — turn it off
and retry. If it's not the VPN, restart Docker Desktop: an `i/o timeout`
specifically on `auth.docker.io` is almost always fixed by that. To check
whether it's Docker:

```bash
curl -sI https://auth.docker.io/token | head -1   # directly from the Mac
docker pull hello-world                            # through Docker
```

If the first one works and the second doesn't, the problem is Docker
Desktop's DNS.

**Rebuilding is almost never needed.** Every service runs code straight from
`backend/` and `frontend/` (the folders are mounted into the container
read-only), so the image only exists for Python and its dependencies.
`docker compose build` is only required when `requirements.txt` changes;
otherwise `docker compose restart` is enough. And if the image was built
from an older `requirements.txt`, the web service fetches the missing
`fastapi`/`uvicorn` from PyPI at startup on its own — pypi.org and
registry.docker.io are different hosts, and the former is usually reachable
even when the latter isn't.

### First thing to run — `doctor`

```bash
docker compose run --rm mcp python cli.py doctor
# or simply: make doctor
```

It checks, in order: the key (its shape, and whether the API actually
responds), reachability of `googleapis.com`, database state, and 24-hour
window coverage — then prints, in plain words, exactly what to fix: YouTube
Data API v3 not enabled, an IP/referrer restriction on the key, exhausted
quota, an empty database, no history. One check costs 1 quota unit.

### CLI: everything, without Claude Desktop

```bash
make cli ARGS="collect-channel @somechannel"      # build up the corpus, cheap
make cli ARGS="collect 'ai automation' --period 24h"
make cli ARGS="refresh"                           # refresh counters → history
make cli ARGS="embed-videos"                      # backfill embeddings, 0 quota
make cli ARGS="viral --period 24h"
make cli ARGS="viral --period 24h --period-by discovered"
make cli ARGS="categories --period 7d --rank-by channels"
make cli ARGS="keywords --period 24h"
make cli ARGS="channels --period 24h"             # outlier channels
make cli ARGS="export-niche brain --out brain.tsv"   # the niche as a TSV table
make cli ARGS="seed"                              # synthetic data, just to look around
```

Useful commands (`make help` shows all of them):

```bash
make up          # bring up the worker
make logs        # watch what it's collecting
make test        # smoke tests inside the image, no key and no network needed
make seed        # load synthetic data and try the tools
make stats       # what's currently in the database
make http        # run MCP over HTTP on :8765 instead of stdio
```

The `mcp` service (i.e. `make cli` and `make doctor`) mounts `./backend`
into the container read-only, so code edits show up immediately, no rebuild
needed. The worker, `mcp-http`, and `scripts/mcp-docker.sh` run code baked
into the image — those need `docker compose build`.

The database lives in the `niche-finder-postgres-data` volume (the
`postgres` service); the embeddings model cache is in `niche-finder-models`.
Rebuilding the image doesn't touch either.

`docker compose build --build-arg PREFETCH_MODEL=1` bakes the embeddings
model (~220 MB) straight into the image, if you don't want to wait for it to
download on the first semantic search.

---

## Running without Docker

```bash
cd ~/Desktop/projects/youtube/analytic/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in YOUTUBE_API_KEY
python3 tests/test_smoke.py   # should be 17/17 passed -- needs a reachable
                              # Postgres (docker compose up -d postgres, or
                              # your own; see infrastructure/postgres/connection.py)
```

Each test process works in its own `nichetest_<random>` schema and drops it on
exit (`tests/schema_scope.py`), so repeated runs leave nothing behind. Set
`NICHE_KEEP_TEST_SCHEMA=1` to keep it and inspect the data after a failure; a
schema you pass in yourself via `NICHE_DB_SCHEMA` is never dropped. Note that
`pytest tests/test_smoke.py tests/test_mcp_tools.py` puts both modules in one
process and therefore one schema, where test_smoke's demo data breaks
test_mcp_tools' cosine-similarity ranking expectation -- run them the way CI
does, one process per file (see .github/workflows/ci.yml).

"Reachable Postgres" means a reachable *host* address, and that is not the
default one: the compose database is published on `127.0.0.1:5433`
(`POSTGRES_HOST_PORT`), while `_dsn()` falls back to `localhost:5432` --
hence `psycopg2.OperationalError: Connection refused` straight out of
`init_db()`. Put the host DSN in the project-root `.env`:

```bash
NICHE_DATABASE_URL=postgresql://niches:niches@localhost:5433/niches
```

It wins over `POSTGRES_*` in `_dsn()` and stays host-only on purpose --
compose does not pass it to containers, and `scripts/mcp-docker.sh` /
`scripts/diag.sh` strip it before `docker run`, since inside a container
`localhost` is the container itself. Do **not** use `POSTGRES_PORT=5433`
instead: compose hands that same variable to the containers as the
in-network port and breaks `web`/`worker`/`mcp`.

Same thing, shorter, via the Makefile (from the project root):

```bash
make local-install   # venv + dependencies, once
make local-test      # smoke tests on the host
make local-run       # MCP server on the host (no Docker)
make dev             # HTTP dashboard on the host: uvicorn api:app --reload on :8080
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "niche-finder": {
      "command": "/path/to/niche-finder/backend/.venv/bin/python3",
      "args": ["/path/to/niche-finder/backend/server.py"]
    }
  }
}
```

History isn't collected automatically in this mode — run `python3 worker.py`
separately (or via cron), otherwise the velocity fields stay empty.

---

## Tools

### Collection (spends quota)

| Tool | What it does | Cost |
|---|---|---|
| `collect_niche` | search by topic → videos + channels + embeddings into the database. `period="24h"` instead of a manual date | 1 search/page out of 100 a day |
| `collect_channel` | a channel's uploads via its uploads playlist; accepts a UC id, @handle, or URL | ~1 unit / 50 videos |
| `collect_trending` | a snapshot of the mostPopular chart (Music/Movies/Gaming) | ~1 unit / page |
| `refresh_stats` | re-read counters and append a snapshot — this is where velocity numbers come from | ~1 unit / 50 videos |
| `refresh_channels` | a snapshot of channel subscribers/views | ~1 unit / 50 channels |
| `refresh_categories` | an up-to-date id → category-name map | 1 unit / region |

### Sections (free, run as much as you like)

| Tool | What it gives you |
|---|---|
| `viral_videos_small_channels` | viral videos on small channels over a period; VSR, age-adjusted outlier, VPH, acceleration |
| `recently_added_outlier_channels` | the same, at the channel level: multiplier + strength band 0–4 |
| `high_future_competition` | young, fast-growing channels about to become your competitors |
| `most_popular_categories` | category ranking over a period + share shift vs. the previous window; `rank_by="views"` or `"channels"` |
| `trending_keywords` | growing phrases with momentum and outlier-lift |
| `search_outliers` | outlier search over the database, semantically ranked by `query` |
| `niche_overview` | niche density: channel-size distribution, viral skew, Shorts share |
| `niche_videos` | every video of a niche as a point: exact date, views, length, channel, both outlier scores, `isOutlier` / `isFresh` — the data behind the dashboard's scatter |
| `list_niches`, `db_stats` | what's been collected |
| `data_coverage` | whether there's enough data for the requested window — call this first if a section comes back empty |

### Channel tracking and analysis

| Tool | What it gives you |
|---|---|
| `track_channel` / `untrack_channel` / `list_tracked_channels` | a watchlist for history |
| `channel_analytics` | profile, cadence, median vs. mean, viral skew, 24h/7d/30d/90d growth, momentum, grade, projections, two revenue models, top outliers |
| `compare_channels` | comparison, ranked by views per subscriber |
| `channel_velocity` | lifetime VPH, 24h VPH, daily gain, "accelerating / decelerating" |
| `title_changes` | who renamed a video or swapped its thumbnail |
| `best_time_to_publish` | 168 weekly slots by median age-adjusted outlier |
| `title_patterns` | which title phrases correlate with breakouts |
| `calibrate_maturity_curve` | recompute the maturity curve from your own data |

### Tagging new videos automatically

Once a taxonomy has settled on hand-tagged videos, the worker can keep it
applied to what arrives afterwards, through OpenRouter. Off by default --
this is the only thing in the project that costs money.

```bash
make cli ARGS="autotag econ --dry-run"          # ask, write nothing
make cli ARGS="autotag econ --group topic_group_econ --limit 50"
```

```
OPENROUTER_API_KEY=...
LLM_MODEL=z-ai/glm-5.3-flash
LLM_TAGGING=1
LLM_TAGGING_NICHES=econ,brain
LLM_TAGGING_MAX_COST_USD=0.25
```

Three properties make it safe to leave running:

* **the taxonomy is closed.** The group's existing tags go into the request as
  a JSON-schema enum and into the prompt as a list, together with a few
  examples of how a human used them. A tag that is not in that list is dropped
  here even if the model returns it, so automatic tagging can never split a
  group into synonyms.
* **it adds, never overwrites.** Rows are written with `source="llm"`, which
  `PROTECTED_BY` in `application/tagging.py` forbids from replacing `manual` or
  `claude-mcp`. A group with fewer than five hand-tagged videos is skipped
  entirely: with nothing to imitate there is nothing to automate.
* **it is bounded.** `limit` videos per run, newest first, in batches, and it
  stops at `LLM_TAGGING_MAX_COST_USD`. Every run reports tokens and dollars,
  and the worker logs one line per niche with the cost.

Two things worth knowing before pointing this at another model:

* **the model id must be exactly what OpenRouter's catalogue says**
  (`https://openrouter.ai/api/v1/models`). A near-miss is a 400, not a
  fallback.
* **a declared JSON schema is a strong hint, not a guarantee.** Support is per
  provider, not per model; requests here carry `strict: true` and
  `provider.require_parameters`, and `z-ai/glm-5.3-flash` still answered inside
  a ```json fence with a shape of its own. Both are handled, and everything is
  re-validated locally.
* **a reasoning model bills for thinking.** On `z-ai/glm-5.3-flash`,
  classifying two videos cost 153 reasoning tokens by default and 27 with
  `reasoning.effort="low"`, which is what the tagger sends. `"none"` is
  rejected outright -- reasoning is mandatory on that endpoint. Tagging 20
  videos measured $0.00037, so a 227-video niche is well under a cent.

### Checking a list of ideas

A brainstorm is a column of nouns, and every one of them asks the same
question: did a competitor already make this video, and did it work?
`check_ideas` answers the whole column in one pass over the corpus.

| Tool | What it does |
|---|---|
| `check_ideas` | one verdict per idea, plus the competitor videos behind it |

```
check_ideas(ideas=["car wash", "funeral home", "laundromat"], niche="econ")
```

| Verdict | Means |
|---|---|
| `free` | nobody in the corpus covered it |
| `recent` | covered within `recent_days` (90 by default) -- a head-on collision, skip it |
| `proven` | covered long ago and it broke out (>= `proven_outlier`) -- demand is proven, saturation is the risk |
| `flopped` | covered long ago and it did not break out |

`recent` is checked before performance on purpose: a video from last month
competes with yours whether it flopped or not, and its own numbers are not
final yet. Performance is judged only on videos older than `fresh_days`, the
same rule the tag hit rate uses.

An idea matches a video two ways, and `matchedBy` says which fired: the phrase
in the title (whole words, punctuation and case ignored) or cosine over the
local embeddings (`min_similarity`, 0.55 by default). Short phrases score low
semantically -- "oil rig" against "The Economics of Owning an Offshore Oil
Rig" sits around 0.5 -- so the title half is what usually catches them, and
`corpus.embedded` reports how much of the corpus the semantic half could even
see. The thresholds are all parameters; every number a verdict was made on
comes back with it.

Zero quota, local database and local embeddings only. The dashboard has the
same thing under "Проверка идей": a textarea and a table of verdicts.

### Topic tags and their hit rate

Your own taxonomy on top of the corpus — not `videos.tags` (those are what
the creator typed on YouTube), but what the breakdown of a niche concluded:
which topic group a video belongs to, which triggers it pulls. Tagging turns
that breakdown from a chat message into data you can rank.

| Tool | What it does |
|---|---|
| `tag_videos` | put `{video_id, tag_group, tag}` triples on videos; `replace=True` for groups where only one tag may stand |
| `untag_videos` | remove those same triples |
| `list_video_tags` | what is tagged already, by niche / video / group, with per-tag video counts |
| `tag_stats` | per tag: `hitRate` (share of its videos that are outliers), `lift` against the niche's own rate, median views, examples |

```
tag_videos(items=[{"video_id": "abc", "tag_group": "topic_group_econ", "tag": "a"}])
tag_stats(niche="econ", tag_group="topic_group_econ")
```

A group may hold several tags per video — three triggers is the normal case.
Single-valued groups ("A or B, never both") are enforced by `replace=True`
at write time, not by the schema.

`source` separates who wrote a tag: `manual` (dashboard), `claude-mcp`
(a conversation), `llm` (automatic tagging). Automatic tagging may add, never
overwrite the first two.

Videos younger than 30 days sit out of both sides of the hit-rate fraction by
default: their views are still coming in, so counting them makes a freshly
explored topic look like a flop. `include_fresh=True` puts them back, and
`freshExcluded` says how many that was either way.

### Exporting a niche

The corpus of one niche as a table, for the research notes in YT-analyze
(`niches/<niche>/data/`): one row per video, with the channel it came from,
both outlier baselines and whatever topic tags it carries.

```bash
make cli ARGS="export-niche brain"                  # videos_YYYY-MM-DD.tsv here
make cli ARGS="export-niche brain --out brain.tsv"
make cli ARGS="export-niche brain --out -"          # to stdout, to pipe onwards
curl -OJ 'http://localhost:8080/api/niches/brain/export.tsv'
```

Columns: `channel, handle, subs, video_id, published_at, views, likes,
comments, length_seconds, is_short, title, outlierScoreRolling,
outlierScorePeriod, tags`. Appended to, never reordered -- a parser on the
other side depends on it. Tags of one video share a cell, as
`group=tag1,tag2; other=tag3`, so a moving taxonomy does not change the
shape of the file.

TSV rather than CSV: titles are full of commas and quotes, and TSV needs no
quoting rules for those. Tabs and newlines inside a field are turned into
spaces, so a split on tabs always yields the same number of columns.

`period` defaults to `all` here, unlike the sections: the export is the
corpus of a niche, not a window into it. Both outlier baselines are always
in the table -- `outlier_base` only picks which one the row filtering thinks
with, and the reader of the file cannot recompute the missing one.

---

## How to use this

**Day one — build up the corpus.** The cheap path: find 20–50 channels in
your topic and load them via `collect_channel` (that's ~1 unit per 50
videos, doesn't spend search). The expensive path, but necessary for
discovering new topics, is `collect_niche`.

```
collect_niche(query="hypotheses about the brain and memory", label="brain", language="en", period="30d", pages=2)
collect_channel(channel="@some-channel", niche="brain")
```

### Important: what "in the last 24 hours" means

Every section has a `period_by` parameter:

- `"published"` (default) — **what came out** in the window. The normal
  human reading.
- `"discovered"` — **what we first saw** in the window.

This isn't pedantry. NexLev's own screenshots show videos tagged "1 year
ago" in a "Viral Videos On Small Channels — Last 24 hours" list — so their
window is about hitting the index, not the publish date. Both modes are
useful: `published` answers "what's new that came out", `discovered`
answers "what's new that I found". To reproduce NexLev's behavior, pass
`period_by="discovered"`.

**Next — look at the sections.** They're free, run them as much as you like:

```
viral_videos_small_channels(period="24h", max_subscribers=10000, sort_by="viral")
viral_videos_small_channels(period="24h", period_by="discovered")   # like NexLev
viral_videos_small_channels(period="all", niche="brain", preset="niche_all")  # every channel of the niche, big ones too
recently_added_outlier_channels(period="24h")
most_popular_categories(period="7d", rank_by="channels")
trending_keywords(period="24h", sort_by="trend")
niche_overview(niche="brain")
```

**Ongoing — keep the worker running.** After a day, `vph24h` and
`viewsGained24h` show up; after a week, channel growth and `momentum`;
after a month, `calibrate_maturity_curve()` recomputes the curve for your
niches.

To have topics refresh themselves, set in `.env`:

```
WORKER_QUERIES=ai automation,faceless history,ai for business
WORKER_QUERY_PERIOD=24h
```

Each topic is one search call a day, so up to ~90 topics is safe.

### An empty result explains itself

`viral_videos_small_channels` returns a `funnel` — how many videos survived
each filter — and a `hint` naming exactly which parameter filtered
everything out:

```json
"funnel": [
  {"step": "videos in window",        "remaining": 64},
  {"step": "channel subs <= 10,000",  "remaining": 33},
  {"step": "video views >= 10,000",   "remaining": 25}
],
"hint": null
```

Every other section returns a `hint` when the result is empty. The CLI
additionally prints the hint as its own line.

**If a section comes back empty**, it's almost never that "nothing is
trending" — it's that nothing was collected for that window.
`data_coverage(period=…)` shows the gap.

---

## Formulas

Full details are in `docs/research-tools.md` (internal notes, not included
in this repository); the code is in `metrics.py`. In short:

```
outlierScoreRolling = views / median views of the previous 10 long-form uploads
outlierScorePeriod  = views / median views of the channel's long-form uploads within ±15 days
                      of publication (uploads younger than 14 days skipped; fewer than 3 in the
                      window -> the channel median, baselinePeriodScope="channel")
outlierScore        = one of the two, picked by outlier_base="rolling" (default) | "period"
outlierScoreAdjusted= views / (baseline * maturity(age in days))
outlierScoreNexlev  = views / (channel.viewCount // channel.videoCount)   # to cross-check against NexLev
viewsPerSubscriber  = views / subscribers
viralScore          = 30-day view projection / subscribers
vphLifetime         = views / hours since publish          # this is what NexLev's UI calls "VPH"
vph24h              = (views_now - views_24h_ago) / 24               # needs history
acceleration        = today's vph24h / yesterday's vph24h            # >1.5 = accelerating
momentum            = views per day over 30d / views per day over lifetime
revenue             = monthly views / 1000 * niche RPM * 0.70
```

Median instead of mean is deliberate: NexLev's baseline is the channel's
lifetime mean, and a single viral video wrecks it (the observed
mean-to-median ratio runs as high as 27x).

The two medians answer different questions. Rolling asks "better than what
the channel did just before?", so a video that follows a hot streak looks like
a flop. Period asks "better than the channel's level at the time?". On a
channel that peaked in July and dropped fivefold in August, a video from the
middle of July scores 0.4x rolling and 0.8x period. Every section, search, niche
and channel tool takes `outlier_base`, and `outlierScore`, `outlierBand`,
`sort_by="outlier"` and `min_outlier_score` follow it; both raw scores are
always in the response. Alerts (`WORKER_ALERTS_OUTLIER_BASE` for the worker),
metadata review and the Chrome extension (its popup setting) take the same
choice; on search-result badges "rolling" stays the cheap channel mean.

---

## Structure

As of 2026-09-05 the backend has been rewritten in DDD/Clean Architecture
layers (domain → infrastructure → application → interfaces), but **every
entry point stayed at its old path**: `server.py`, `api.py`, `cli.py`,
`worker.py` in `backend/` are thin shims (a composition root) that just
import the real code from its new home. So `docker compose up`,
`python cli.py ...`, `uvicorn api:app`, and the whole Makefile work
unchanged. The old flat modules (`db.py`, `trends.py`, `query.py`, etc.)
were kept for a while in `backend/_legacy_flat_modules/` as a reference,
but nothing in the code referenced them, so that directory has been removed.

```
analytic/
├── docker-compose.yml      worker + MCP (stdio and HTTP profile)
├── Makefile                make up / logs / test / seed / stats / dev
├── .env.example            key and worker settings
├── scripts/mcp-docker.sh   MCP launcher in Docker for Claude Desktop
├── frontend/               dashboard: index.html, styles.css, ui.js, app.js
├── docs/                   internal notes, not included in this repository
│   ├── context.md          project decision history
│   └── research-tools.md   market research, formulas, what's reproducible
└── backend/
    ├── Dockerfile
    ├── server.py           shim: python server.py -> interfaces.mcp.server
    ├── cli.py              shim: python cli.py ...  -> interfaces.cli.cli
    ├── api.py              shim: uvicorn api:app    -> interfaces.http.api
    ├── worker.py           shim: python worker.py   -> application.worker_cycle
    ├── migrate_sqlite_to_postgres.py   one-off migration from the old niches.db
    │
    ├── domain/             pure rules, no external dependencies
    │   ├── metrics.py          every formula (outlier, VPH, revenue, ...)
    │   ├── periods.py          parsing 24h / 7d / 30d / all
    │   ├── keywords.py         n-grams, momentum, lift
    │   ├── tag_stats.py        hit rate and lift of a topic tag
    │   ├── scoring.py          backward compatibility (see metrics.py)
    │   └── categories_catalog.py  pure YouTube categories + offline fallback
    │
    ├── infrastructure/     adapters to the outside world
    │   ├── postgres/           connection.py, schema.py, repositories.py
    │   │                       (Postgres schema v4, sqlite3-compatible shim)
    │   ├── youtube/client.py   wrapper around YouTube Data API v3 + quota model
    │   ├── embeddings/fastembed_provider.py  local multilingual embeddings
    │   └── categories/repository.py          categories, cached in Postgres + YouTube API
    │
    ├── application/        use-case orchestration
    │   ├── collecting.py       everything that spends YouTube quota (was collector.py)
    │   ├── discovery.py        the three period-based sections (was trends.py)
    │   ├── channel_tracking.py channel tracking and analysis (was tracking.py)
    │   ├── search.py           outlier search and niche overview (was query.py)
    │   ├── tagging.py          topic tags on videos and their hit rate
    │   └── worker_cycle.py     the background collector's loop (was worker.py)
    │
    ├── interfaces/         thin adapters facing outward
    │   ├── mcp/server.py       MCP server, 48 tools
    │   ├── http/api.py         HTTP API for the dashboard (FastAPI)
    │   ├── cli/cli.py          same, from the terminal, plus doctor (diagnostics)
    │   └── worker/main.py      background collector's entry point
    │
    └── tests/              smoke tests and the synthetic seed
```
