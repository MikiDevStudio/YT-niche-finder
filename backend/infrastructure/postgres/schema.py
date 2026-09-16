"""DDL and migration bookkeeping for the niche-finder schema.

Schema v2 adds everything needed for *time-window* analytics (24h / 7d / 30d):
history snapshots of video and channel stats, a tracked-channel watchlist,
chart snapshots, and title/thumbnail change detection.

Schema v3 (iteration 8) adds the swipe file (saved_items), metadata-review
drafts with their post-publish outcome (drafts), and worker-generated
alerts (events) -- see docs/plan-iteration-8.md.
"""
from datetime import datetime, timezone

from domain.channel_refs import is_channel_id, parse_channel_ref
from infrastructure.postgres.connection import get_conn  # noqa: F401  (re-export for callers)

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    title TEXT,
    custom_url TEXT,
    country TEXT,
    description TEXT,
    default_language TEXT,
    subscriber_count BIGINT,
    video_count BIGINT,
    view_count BIGINT,
    thumbnail TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT,
    title TEXT,
    description TEXT,
    published_at TEXT,
    duration_seconds INTEGER,
    view_count BIGINT,
    like_count BIGINT,
    comment_count BIGINT,
    thumbnail TEXT,
    tags TEXT,
    default_language TEXT,
    embedding BYTEA,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS niches (
    slug TEXT PRIMARY KEY,
    query TEXT,
    label TEXT,
    created_at TEXT,
    last_collected_at TEXT
);

CREATE TABLE IF NOT EXISTS video_niches (
    video_id TEXT,
    niche_slug TEXT,
    PRIMARY KEY (video_id, niche_slug)
);

-- ---------- v2: history / tracking ----------

CREATE TABLE IF NOT EXISTS video_stats_history (
    video_id TEXT,
    captured_at TEXT,
    view_count BIGINT,
    like_count BIGINT,
    comment_count BIGINT,
    title TEXT,
    thumbnail TEXT,
    PRIMARY KEY (video_id, captured_at)
);

CREATE TABLE IF NOT EXISTS channel_stats_history (
    channel_id TEXT,
    captured_at TEXT,
    subscriber_count BIGINT,
    video_count BIGINT,
    view_count BIGINT,
    PRIMARY KEY (channel_id, captured_at)
);

CREATE TABLE IF NOT EXISTS tracked_channels (
    channel_id TEXT PRIMARY KEY,
    note TEXT,
    added_at TEXT,
    last_refreshed_at TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chart_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    captured_at TEXT,
    region TEXT,
    category_id TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS chart_entries (
    snapshot_id BIGINT,
    video_id TEXT,
    rank INTEGER,
    view_count BIGINT,
    PRIMARY KEY (snapshot_id, video_id)
);

CREATE TABLE IF NOT EXISTS video_changes (
    video_id TEXT,
    changed_at TEXT,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    PRIMARY KEY (video_id, changed_at, field)
);

CREATE TABLE IF NOT EXISTS video_categories (
    category_id TEXT,
    region TEXT,
    title TEXT,
    assignable INTEGER,
    updated_at TEXT,
    PRIMARY KEY (category_id, region)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at);
CREATE INDEX IF NOT EXISTS idx_video_niches_slug ON video_niches(niche_slug);
CREATE INDEX IF NOT EXISTS idx_vsh_video ON video_stats_history(video_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_csh_channel ON channel_stats_history(channel_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_chart_snap ON chart_snapshots(captured_at, region, category_id);

-- ---------- v3: swipe file, metadata-review drafts, worker alerts ----------

CREATE TABLE IF NOT EXISTS saved_items (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT,              -- 'video' | 'channel'
    ref_id TEXT,             -- video_id or channel_id
    folder TEXT,             -- user folder, defaults to 'default'
    note TEXT,
    payload TEXT,            -- JSON snapshot of metrics at save time
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id BIGSERIAL PRIMARY KEY,
    video_id TEXT,           -- filled in once the draft is published and linked
    title TEXT,
    description TEXT,
    tags TEXT,               -- JSON list
    niche TEXT,
    channel_id TEXT,
    is_short INTEGER,
    review TEXT,             -- JSON snapshot of the signal review at save time
    created_at TEXT,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT,               -- 'outlier' | 'acceleration' | 'title_change' | ...
    ref_id TEXT,              -- video_id or channel_id the event is about
    payload TEXT,             -- JSON details
    created_at TEXT,
    seen_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_saved_items_kind_ref ON saved_items(kind, ref_id);
CREATE INDEX IF NOT EXISTS idx_drafts_video ON drafts(video_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_seen ON events(seen_at);
"""

# columns added to pre-existing tables (name -> DDL type)
MIGRATIONS = {
    "videos": {
        "category_id": "TEXT",
        "region": "TEXT",
        "is_short": "INTEGER",
        "topic_categories": "TEXT",
        "first_seen_at": "TEXT",
        "live_content": "TEXT",
        "contains_synthetic_media": "INTEGER",
    },
    "channels": {
        "published_at": "TEXT",
        "topic_categories": "TEXT",
        "keywords": "TEXT",
        "uploads_playlist": "TEXT",
        "first_seen_at": "TEXT",
        "hidden_subs": "INTEGER",
    },
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _existing_columns(conn, table):
    return {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?", (table,)
    ).fetchall()}


def migrate(conn):
    """Add any v2 columns missing from a v1 database. Safe to run every start."""
    added = []
    for table, cols in MIGRATIONS.items():
        have = _existing_columns(conn, table)
        if not have:
            continue
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                added.append(f"{table}.{col}")
    _backfill_niche_rows(conn)
    _rekey_tracked_handles(conn)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return added


def _backfill_niche_rows(conn):
    """Give every slug in video_niches a row in niches.

    collect_channel(niche=...) used to link videos without creating the niche
    row, so list_niches and the dashboard's niche picker never saw those
    niches. The collection date is unknown for such rows -- now() stands in."""
    ts = now_iso()
    conn.execute(
        "INSERT INTO niches (slug, query, label, created_at, last_collected_at) "
        "SELECT DISTINCT vn.niche_slug, NULL, vn.niche_slug, ?, ? "
        "FROM video_niches vn LEFT JOIN niches n ON n.slug = vn.niche_slug "
        "WHERE n.slug IS NULL "
        "ON CONFLICT (slug) DO NOTHING",
        (ts, ts),
    )


def _rekey_tracked_handles(conn):
    """Re-key watchlist rows saved under a raw @handle or URL to the UC id.

    track_channel(collect=False) used to store the ref as typed (issue #14),
    and channels.list?id= knows nothing about handles, so the worker never
    snapshotted those rows. Only channels already in `channels` are repaired,
    since a startup migration must not spend quota; the rest stay until the
    channel is tracked again, which resolves it and drops the old row.
    """
    # repositories imports this module, hence the call-time import
    from infrastructure.postgres.repositories import channel_id_for_handle

    rows = conn.execute(
        "SELECT channel_id, note, added_at, last_refreshed_at, active FROM tracked_channels"
    ).fetchall()
    for r in rows:
        ref = r["channel_id"]
        parsed = None if is_channel_id(ref) else parse_channel_ref(ref)
        if parsed is None:
            continue
        kind, value = parsed
        cid = value if kind == "id" else channel_id_for_handle(conn, value)
        if not cid:
            continue
        conn.execute(
            "INSERT INTO tracked_channels (channel_id, note, added_at, last_refreshed_at, active) "
            "VALUES (?,?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET "
            "active=GREATEST(tracked_channels.active, excluded.active), "
            "note=COALESCE(tracked_channels.note, excluded.note)",
            (cid, r["note"], r["added_at"], r["last_refreshed_at"], r["active"]),
        )
        conn.execute("DELETE FROM tracked_channels WHERE channel_id=?", (ref,))


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    conn.close()
