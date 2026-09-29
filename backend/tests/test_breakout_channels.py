"""Tests for the channel-level breakout section (channel_tracking.recently_added_outlier_channels).
sqlite double for infrastructure.postgres, no YouTube key.

Run: python3 tests/test_breakout_channels.py
"""
import os
import sys
import sqlite3
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

NOW = datetime.now(timezone.utc)
iso = lambda dt: dt.isoformat()

SCHEMA = """
CREATE TABLE channels (
  channel_id TEXT PRIMARY KEY, title TEXT, custom_url TEXT, country TEXT,
  subscriber_count INTEGER, video_count INTEGER, view_count INTEGER, published_at TEXT
);
CREATE TABLE videos (
  video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, tags TEXT,
  published_at TEXT, first_seen_at TEXT, view_count INTEGER, like_count INTEGER,
  comment_count INTEGER, duration_seconds INTEGER, thumbnail TEXT,
  category_id TEXT, region TEXT, default_language TEXT, is_short INTEGER,
  contains_synthetic_media INTEGER, embedding BLOB, updated_at TEXT
);
CREATE TABLE video_niches (video_id TEXT, niche_slug TEXT, PRIMARY KEY(video_id, niche_slug));
CREATE TABLE video_stats_history (video_id TEXT, captured_at TEXT, view_count INTEGER);
CREATE TABLE video_tags (video_id TEXT, tag_group TEXT, tag TEXT, source TEXT, created_at TEXT,
  PRIMARY KEY (video_id, tag_group, tag));
"""

RAW = None


class _Conn:
    def execute(self, sql, params=()):
        return RAW.execute(sql, params)

    def commit(self):
        RAW.commit()

    def close(self):
        pass


def reset():
    global RAW
    RAW = sqlite3.connect(":memory:")
    RAW.row_factory = sqlite3.Row
    RAW.executescript(SCHEMA)


reset()
fake_db = types.ModuleType("infrastructure.postgres")
fake_db.get_conn = lambda: _Conn()
fake_db.now_iso = lambda: iso(datetime.now(timezone.utc))
sys.modules["infrastructure.postgres"] = fake_db

import application.channel_tracking as T  # noqa: E402


def channel(cid, created_days_ago, videos=100, views=10_000_000):
    RAW.execute("INSERT INTO channels (channel_id, title, custom_url, subscriber_count,"
                " video_count, view_count, published_at) VALUES (?,?,?,?,?,?,?)",
                (cid, cid, "@" + cid.lower(), 50_000, videos, views,
                 iso(NOW - timedelta(days=created_days_ago))))


def video(vid, cid, days_ago, views, first_seen_days_ago=None):
    seen = first_seen_days_ago if first_seen_days_ago is not None else days_ago
    RAW.execute("INSERT INTO videos (video_id, channel_id, title, tags, published_at, first_seen_at,"
                " view_count, duration_seconds, is_short, category_id) VALUES (?,?,?,?,?,?,?,?,0,?)",
                (vid, cid, f"title {vid}", "[]", iso(NOW - timedelta(days=days_ago)),
                 iso(NOW - timedelta(days=seen)), views, 900, "26"))


def history(cid, n=5, views=10_000, start_days_ago=120):
    for i in range(n):
        video(f"{cid}-h{i}", cid, start_days_ago + i, views)


def ranked(**kw):
    kw.setdefault("period", "30d")
    kw.setdefault("min_multiplier", 1.5)
    return T.recently_added_outlier_channels(**kw)


def test_old_hit_found_recently_is_not_in_a_30_day_window():
    reset()
    channel("UCrusty", created_days_ago=5500, videos=9951, views=57_700_000)
    history("UCrusty", start_days_ago=1900)   # a real baseline: only the window may exclude it
    video("vrusty", "UCrusty", days_ago=1780, views=13_900_000, first_seen_days_ago=14)
    RAW.commit()
    assert ranked()["channels"] == []


def test_channel_without_own_baseline_is_left_out():
    reset()
    channel("UClonely", created_days_ago=400)
    video("vlonely", "UClonely", days_ago=5, views=5_000_000)   # no history at all
    RAW.commit()
    res = ranked()
    assert res["channels"] == []
    assert res["channelsWithoutBaseline"] == 1


def test_series_of_hits_is_not_beaten_by_one_mega_hit():
    reset()
    channel("UCseries", created_days_ago=800)
    history("UCseries")
    for i in range(3):
        video(f"vs{i}", "UCseries", days_ago=10 + i, views=40_000)      # ~4x each
    channel("UCmega", created_days_ago=800)
    history("UCmega")
    video("vmega", "UCmega", days_ago=10, views=640_000)                # ~64x once
    RAW.commit()
    chans = {c["channelId"]: c for c in ranked()["channels"]}
    assert chans["UCseries"]["hitsInWindow"] >= 3
    assert chans["UCseries"]["breakoutScore"] >= chans["UCmega"]["breakoutScore"] * 0.9


def test_young_channel_ranks_above_old_one_with_the_same_hits():
    reset()
    for cid, age in (("UCyoung", 90), ("UCold", 5000)):
        channel(cid, created_days_ago=age)
        history(cid, start_days_ago=60)
        video(f"v{cid}", cid, days_ago=10, views=80_000)
    RAW.commit()
    order = [c["channelId"] for c in ranked()["channels"]]
    assert order.index("UCyoung") < order.index("UCold"), order
    young = next(c for c in ranked()["channels"] if c["channelId"] == "UCyoung")
    assert young["youthFactor"] == 2.0 and young["channelAgeDays"] in (89, 90, 91)


def test_videos_in_db_is_what_we_stored_not_the_youtube_total():
    reset()
    channel("UCcount", created_days_ago=400, videos=683)
    history("UCcount", n=5)
    video("vcount", "UCcount", days_ago=5, views=200_000)
    RAW.commit()
    c = ranked()["channels"][0]
    assert c["videosInDb"] == 6
    assert c["channelVideoCount"] == 683


def test_best_video_carries_its_topic_tags():
    reset()
    channel("UCtag", created_days_ago=100)
    history("UCtag")
    video("vtag", "UCtag", days_ago=5, views=300_000)
    RAW.execute("INSERT INTO video_tags VALUES ('vtag','garden_topic','perennials','manual','x')")
    RAW.commit()
    best = ranked()["channels"][0]["bestVideo"]
    assert best["videoId"] == "vtag"
    assert best["tags"] == [{"group": "garden_topic", "tag": "perennials"}]


def test_best_time_ignores_videos_without_a_baseline():
    reset()
    channel("UCnobase", created_days_ago=400)
    video("vnobase", "UCnobase", days_ago=5, views=5_000_000)   # VSR 100, no own median
    RAW.commit()
    res = T.best_time_to_publish(period="30d", min_samples=1)
    assert res["heatmap"] == [], res


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  FAIL  {fn.__name__}: {e!r}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
