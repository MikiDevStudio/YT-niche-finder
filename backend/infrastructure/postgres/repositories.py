"""Upsert / record / tracking functions -- the write-side repository API.

All functions take an already-open connection (from connection.get_conn())
as their first argument, matching the original db.py call convention used
throughout application/* and interfaces/*.
"""
from infrastructure.postgres.schema import now_iso

_CHANNEL_COLS = [
    "channel_id", "title", "custom_url", "country", "description", "default_language",
    "subscriber_count", "video_count", "view_count", "thumbnail", "updated_at",
    "published_at", "topic_categories", "keywords", "uploads_playlist", "hidden_subs",
]

_VIDEO_COLS = [
    "video_id", "channel_id", "title", "description", "published_at", "duration_seconds",
    "view_count", "like_count", "comment_count", "thumbnail", "tags", "default_language",
    "embedding", "updated_at", "category_id", "region", "is_short", "topic_categories",
    "live_content", "contains_synthetic_media",
]


def _upsert(conn, table, pk, cols, row, coalesce=()):
    """Generic upsert keeping NULLs in `coalesce` columns from overwriting data."""
    data = {c: row.get(c) for c in cols}
    placeholders = ",".join(f"%({c})s" for c in cols)
    updates = []
    for c in cols:
        if c == pk:
            continue
        if c in coalesce:
            updates.append(f"{c}=COALESCE(excluded.{c}, {table}.{c})")
        else:
            updates.append(f"{c}=excluded.{c}")
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {','.join(updates)}"
    )
    cur = conn._conn.cursor()
    cur.execute(sql, data)
    cur.close()


def upsert_channel(conn, ch: dict):
    ch.setdefault("first_seen_at", ch.get("updated_at") or now_iso())
    conn.execute(
        "INSERT INTO channels (channel_id, first_seen_at) VALUES (?,?) "
        "ON CONFLICT (channel_id) DO NOTHING",
        (ch["channel_id"], ch["first_seen_at"]),
    )
    _upsert(conn, "channels", "channel_id", _CHANNEL_COLS, ch,
            coalesce=("published_at", "topic_categories", "keywords", "uploads_playlist"))


def upsert_video(conn, v: dict):
    v.setdefault("first_seen_at", v.get("updated_at") or now_iso())
    conn.execute(
        "INSERT INTO videos (video_id, first_seen_at) VALUES (?,?) "
        "ON CONFLICT (video_id) DO NOTHING",
        (v["video_id"], v["first_seen_at"]),
    )
    _upsert(conn, "videos", "video_id", _VIDEO_COLS, v,
            coalesce=("embedding", "category_id", "region", "topic_categories", "tags"))


def record_video_stats(conn, video_id, view_count, like_count, comment_count,
                       title=None, thumbnail=None, captured_at=None):
    """Append a stats snapshot and log title/thumbnail changes (1of10-style)."""
    captured_at = captured_at or now_iso()
    prev = conn.execute(
        "SELECT title, thumbnail FROM video_stats_history WHERE video_id=? "
        "ORDER BY captured_at DESC LIMIT 1",
        (video_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO video_stats_history "
        "(video_id, captured_at, view_count, like_count, comment_count, title, thumbnail) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT (video_id, captured_at) DO UPDATE SET "
        "view_count=excluded.view_count, like_count=excluded.like_count, "
        "comment_count=excluded.comment_count, title=excluded.title, "
        "thumbnail=excluded.thumbnail",
        (video_id, captured_at, view_count, like_count, comment_count, title, thumbnail),
    )
    if prev:
        for field, old, new in (("title", prev["title"], title),
                                ("thumbnail", prev["thumbnail"], thumbnail)):
            if old and new and old != new:
                conn.execute(
                    "INSERT INTO video_changes "
                    "(video_id, changed_at, field, old_value, new_value) VALUES (?,?,?,?,?) "
                    "ON CONFLICT (video_id, changed_at, field) DO NOTHING",
                    (video_id, captured_at, field, old, new),
                )


def record_channel_stats(conn, channel_id, subscriber_count, video_count, view_count,
                         captured_at=None):
    conn.execute(
        "INSERT INTO channel_stats_history "
        "(channel_id, captured_at, subscriber_count, video_count, view_count) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT (channel_id, captured_at) DO UPDATE SET "
        "subscriber_count=excluded.subscriber_count, video_count=excluded.video_count, "
        "view_count=excluded.view_count",
        (channel_id, captured_at or now_iso(), subscriber_count, video_count, view_count),
    )


def link_video_niche(conn, video_id: str, niche_slug: str):
    conn.execute(
        "INSERT INTO video_niches (video_id, niche_slug) VALUES (?, ?) "
        "ON CONFLICT (video_id, niche_slug) DO NOTHING",
        (video_id, niche_slug),
    )


def upsert_niche(conn, slug: str, query: str, label: str):
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO niches (slug, query, label, created_at, last_collected_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET last_collected_at=excluded.last_collected_at
        """,
        (slug, query, label, ts, ts),
    )


def track_channel(conn, channel_id: str, note: str = None):
    conn.execute(
        "INSERT INTO tracked_channels (channel_id, note, added_at, active) VALUES (?,?,?,1) "
        "ON CONFLICT(channel_id) DO UPDATE SET active=1, note=COALESCE(excluded.note, tracked_channels.note)",
        (channel_id, note, now_iso()),
    )


def untrack_channel(conn, channel_id: str):
    conn.execute("UPDATE tracked_channels SET active=0 WHERE channel_id=?", (channel_id,))


def channel_id_for_handle(conn, handle: str):
    """UC id of an already stored channel by its handle -- no quota.

    custom_url is kept as channels.list returns it ("@name"); older channels
    can still carry a legacy vanity name without the "@"."""
    h = handle.lstrip("@").lower()
    row = conn.execute(
        "SELECT channel_id FROM channels WHERE lower(custom_url) IN (?, ?) LIMIT 1",
        ("@" + h, h),
    ).fetchone()
    return row["channel_id"] if row else None


def new_chart_snapshot(conn, region: str, category_id: str, source: str) -> int:
    row = conn.execute(
        "INSERT INTO chart_snapshots (captured_at, region, category_id, source) "
        "VALUES (?,?,?,?) RETURNING snapshot_id",
        (now_iso(), region, category_id, source),
    ).fetchone()
    return row["snapshot_id"]


def add_chart_entry(conn, snapshot_id: int, video_id: str, rank: int, view_count: int):
    conn.execute(
        "INSERT INTO chart_entries (snapshot_id, video_id, rank, view_count) VALUES (?,?,?,?) "
        "ON CONFLICT (snapshot_id, video_id) DO UPDATE SET "
        "rank=excluded.rank, view_count=excluded.view_count",
        (snapshot_id, video_id, rank, view_count),
    )


def get_meta(conn, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key: str, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def upsert_category(conn, category_id, region, title, assignable):
    conn.execute(
        "INSERT INTO video_categories (category_id, region, title, assignable, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(category_id, region) DO UPDATE SET "
        "title=excluded.title, assignable=excluded.assignable, updated_at=excluded.updated_at",
        (str(category_id), region, title, 1 if assignable else 0, now_iso()),
    )


# ------------------------------------------------ topic tags (issue #2)
#
# `protect_sources` is what keeps automatic tagging from overwriting human
# work: the writer names the sources it must not touch, and both the replace
# sweep and the upsert skip rows that carry them. The auto-tagger of #7 passes
# ("manual", "claude-mcp"); a human passes nothing and wins over everything.

def _tag_keys(items):
    return [(i["video_id"], i["tag_group"], i["tag"]) for i in items]


def upsert_video_tags(conn, items, source: str, replace: bool = False,
                      protect_sources=()) -> dict:
    """Write {video_id, tag_group, tag} rows. Returns counts, never raises on
    a tag that is already there -- the same call twice changes nothing.

    replace=True first drops the other tags of the touched (video, group)
    pairs, so re-tagging a video from group A to group B does not leave it in
    both. It only sweeps the groups actually present in `items`.
    """
    if not items:
        return {"written": 0, "removed": 0, "skipped": 0}
    now = now_iso()
    removed = 0

    if replace:
        keep = {}
        for video_id, group, tag in _tag_keys(items):
            keep.setdefault((video_id, group), set()).add(tag)
        for (video_id, group), tags in keep.items():
            sql = ("DELETE FROM video_tags WHERE video_id=? AND tag_group=? "
                   f"AND tag NOT IN ({','.join('?' * len(tags))})")
            params = [video_id, group, *sorted(tags)]
            if protect_sources:
                sql += f" AND source NOT IN ({','.join('?' * len(protect_sources))})"
                params += list(protect_sources)
            removed += conn.execute(sql, params).rowcount

    sql = ("INSERT INTO video_tags (video_id, tag_group, tag, source, created_at) "
           "VALUES (?,?,?,?,?) ON CONFLICT (video_id, tag_group, tag) DO UPDATE SET "
           "source=excluded.source")
    if protect_sources:
        sql += f" WHERE video_tags.source NOT IN ({','.join('?' * len(protect_sources))})"
    written = skipped = 0
    for video_id, group, tag in _tag_keys(items):
        params = [video_id, group, tag, source, now]
        if protect_sources:
            params += list(protect_sources)
        if conn.execute(sql, params).rowcount:
            written += 1
        else:
            skipped += 1
    return {"written": written, "removed": removed, "skipped": skipped}


def delete_video_tags(conn, items) -> dict:
    """Remove the listed {video_id, tag_group, tag} rows."""
    removed = 0
    for video_id, group, tag in _tag_keys(items):
        removed += conn.execute(
            "DELETE FROM video_tags WHERE video_id=? AND tag_group=? AND tag=?",
            (video_id, group, tag),
        ).rowcount
    return {"removed": removed}


def video_tags_for(conn, video_ids=None, niche: str = None, tag_group: str = None):
    """Tag rows for a set of videos, a whole niche, or one group of either."""
    sql = ("SELECT t.video_id, t.tag_group, t.tag, t.source, t.created_at "
           "FROM video_tags t")
    where, params = [], []
    if niche:
        sql += " JOIN video_niches vn ON vn.video_id = t.video_id"
        where.append("vn.niche_slug = ?")
        params.append(niche)
    if video_ids is not None:
        ids = list(video_ids)
        if not ids:
            return []
        where.append(f"t.video_id IN ({','.join('?' * len(ids))})")
        params += ids
    if tag_group:
        where.append("t.tag_group = ?")
        params.append(tag_group)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.video_id, t.tag_group, t.tag"
    return conn.execute(sql, params).fetchall()
