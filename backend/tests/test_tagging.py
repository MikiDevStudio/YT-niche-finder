"""Tests for application/tagging.py and the video_tags repository (issue #2).

sqlite double, no key, no Postgres -- same pattern as test_library.py, except
that the real repository SQL is loaded and run unmodified (as test_top_tags.py
does), because the upsert/replace/protect logic IS the thing under test.

application.search is stubbed: tag_stats' job here is wiring the niche's video
rows into domain/tag_stats, and the real niche_videos is covered end to end in
test_mcp_tools.py against Postgres.

Run: python3 tests/test_tagging.py (or pytest tests/test_tagging.py)
"""
import os
import sys
import sqlite3
import types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SCHEMA = """
CREATE TABLE video_tags (
  video_id TEXT, tag_group TEXT, tag TEXT, source TEXT, created_at TEXT,
  PRIMARY KEY (video_id, tag_group, tag)
);
CREATE TABLE video_niches (video_id TEXT, niche_slug TEXT, PRIMARY KEY(video_id, niche_slug));
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

# The facade module the application layer imports, backed by sqlite. __path__
# makes it a package so the real repositories module can still be imported
# through it -- with schema.py replaced, so psycopg2 is never touched.
fake_db = types.ModuleType("infrastructure.postgres")
fake_db.__path__ = [os.path.join(ROOT, "infrastructure", "postgres")]
fake_db.get_conn = lambda: _Conn()
fake_db.now_iso = lambda: datetime.now(timezone.utc).isoformat()
sys.modules["infrastructure.postgres"] = fake_db

fake_schema = types.ModuleType("infrastructure.postgres.schema")
fake_schema.now_iso = fake_db.now_iso
sys.modules["infrastructure.postgres.schema"] = fake_schema

import infrastructure.postgres.repositories as repo  # noqa: E402  (real SQL)

for _name in ("upsert_video_tags", "delete_video_tags", "video_tags_for"):
    setattr(fake_db, _name, getattr(repo, _name))

NICHE_VIDEOS = {"videos": [], "outlierBase": "rolling", "hint": None}

fake_search = types.ModuleType("application.search")
fake_search.niche_videos = lambda **kw: dict(NICHE_VIDEOS, _called_with=kw)
sys.modules["application.search"] = fake_search
import application  # noqa: E402

application.search = fake_search

import application.tagging as TG  # noqa: E402

ITEM = {"video_id": "vid1", "tag_group": "topic_group_econ", "tag": "a"}


def rows():
    return [dict(r) for r in RAW.execute(
        "SELECT video_id, tag_group, tag, source FROM video_tags "
        "ORDER BY video_id, tag_group, tag").fetchall()]


def test_tag_and_list_roundtrip():
    reset()
    res = TG.tag_videos([ITEM, {"video_id": "vid2", "tag_group": "topic_group_econ",
                                "tag": "b"}])
    assert res["written"] == 2 and res["videos"] == 2 and res["quota"] == 0
    assert res["source"] == "claude-mcp"

    listed = TG.list_video_tags()
    assert listed["count"] == 2
    assert listed["groups"] == [{"group": "topic_group_econ", "videos": 2,
                                 "tags": [{"tag": "a", "videos": 1},
                                          {"tag": "b", "videos": 1}]}]
    assert listed["items"][0] == {"videoId": "vid1", "group": "topic_group_econ",
                                  "tag": "a", "source": "claude-mcp",
                                  "createdAt": listed["items"][0]["createdAt"]}


def test_tagging_twice_changes_nothing():
    reset()
    TG.tag_videos([ITEM])
    TG.tag_videos([ITEM])
    assert len(rows()) == 1


def test_multi_valued_group_keeps_every_tag():
    """Three triggers on one video is the normal case, not a conflict."""
    reset()
    TG.tag_videos([{"video_id": "vid1", "tag_group": "trigger", "tag": t}
                   for t in ("curiosity", "recognition", "fantasy")])
    assert len(rows()) == 3


def test_replace_clears_only_the_touched_group_and_videos():
    reset()
    TG.tag_videos([{"video_id": "vid1", "tag_group": "topic_group_econ", "tag": "a"},
                   {"video_id": "vid1", "tag_group": "trigger", "tag": "curiosity"},
                   {"video_id": "vid2", "tag_group": "topic_group_econ", "tag": "a"}])
    res = TG.tag_videos([{"video_id": "vid1", "tag_group": "topic_group_econ",
                          "tag": "b"}], replace=True)
    assert res["removed"] == 1                       # vid1's old 'a' is gone
    assert rows() == [
        {"video_id": "vid1", "tag_group": "topic_group_econ", "tag": "b",
         "source": "claude-mcp"},
        {"video_id": "vid1", "tag_group": "trigger", "tag": "curiosity",
         "source": "claude-mcp"},                     # other group untouched
        {"video_id": "vid2", "tag_group": "topic_group_econ", "tag": "a",
         "source": "claude-mcp"},                     # other video untouched
    ]


def test_replace_keeps_the_tags_it_is_writing():
    """Re-sending the same tag with replace=True must not delete and lose it."""
    reset()
    TG.tag_videos([ITEM])
    TG.tag_videos([ITEM], replace=True)
    assert rows() == [{"video_id": "vid1", "tag_group": "topic_group_econ",
                       "tag": "a", "source": "claude-mcp"}]


def test_llm_never_overwrites_a_human_tag():
    """The condition issue #7 stands on: automatic tagging may add, never
    overwrite what a person or a conversation put there."""
    reset()
    TG.tag_videos([ITEM], source="manual")
    res = TG.tag_videos([ITEM, {"video_id": "vid9", "tag_group": "topic_group_econ",
                                "tag": "c"}], source="llm")
    assert res["written"] == 1 and res["skipped"] == 1
    assert rows()[0]["source"] == "manual"
    assert rows()[1] == {"video_id": "vid9", "tag_group": "topic_group_econ",
                         "tag": "c", "source": "llm"}


def test_llm_replace_does_not_sweep_away_protected_rows():
    reset()
    TG.tag_videos([ITEM], source="claude-mcp")
    res = TG.tag_videos([{"video_id": "vid1", "tag_group": "topic_group_econ",
                          "tag": "b"}], source="llm", replace=True)
    assert res["removed"] == 0
    assert {r["tag"] for r in rows()} == {"a", "b"}


def test_a_human_does_overwrite_an_llm_tag():
    reset()
    TG.tag_videos([ITEM], source="llm")
    TG.tag_videos([ITEM], source="manual")
    assert rows()[0]["source"] == "manual"


def test_untag_removes_one_row_only():
    reset()
    TG.tag_videos([ITEM, {"video_id": "vid1", "tag_group": "trigger",
                          "tag": "curiosity"}])
    res = TG.untag_videos([ITEM])
    assert res["removed"] == 1
    assert [r["tag_group"] for r in rows()] == ["trigger"]


def test_untagging_something_absent_is_not_an_error():
    reset()
    assert TG.untag_videos([ITEM])["removed"] == 0


def test_groups_and_tags_are_lowercased_video_ids_are_not():
    """YouTube ids are case-sensitive; a taxonomy that splits on
    capitalisation is worse than useless."""
    reset()
    TG.tag_videos([{"video_id": " dQw4w9WgXcQ ", "tag_group": " Topic_Group_Econ ",
                    "tag": " Группа А "}])
    assert rows() == [{"video_id": "dQw4w9WgXcQ", "tag_group": "topic_group_econ",
                       "tag": "группа а", "source": "claude-mcp"}]


def test_camel_case_keys_are_accepted_too():
    """The dashboard speaks camelCase, MCP callers snake_case."""
    reset()
    TG.tag_videos([{"videoId": "vid1", "tagGroup": "trigger", "tag": "curiosity"}])
    assert len(rows()) == 1


def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError:
        return True
    return False


def test_validation():
    reset()
    assert _raises(TG.tag_videos, [])
    assert _raises(TG.tag_videos, "not a list")
    assert _raises(TG.tag_videos, [ITEM], source="banana")
    assert _raises(TG.tag_videos, [{"tag_group": "g", "tag": "t"}])
    assert _raises(TG.tag_videos, [{"video_id": "v", "tag_group": "g", "tag": " "}])
    assert _raises(TG.tag_videos, [{"video_id": "v", "tag_group": "g", "tag": "x" * 65}])
    assert _raises(TG.tag_videos, [ITEM] * (TG.MAX_ITEMS + 1))
    assert rows() == []                              # nothing was half-written


def test_list_filters():
    reset()
    TG.tag_videos([{"video_id": "vid1", "tag_group": "topic_group_econ", "tag": "a"},
                   {"video_id": "vid1", "tag_group": "trigger", "tag": "curiosity"},
                   {"video_id": "vid2", "tag_group": "trigger", "tag": "fantasy"}])
    RAW.execute("INSERT INTO video_niches VALUES ('vid1','econ')")
    RAW.commit()
    assert TG.list_video_tags(video_id="vid1")["count"] == 2
    assert TG.list_video_tags(tag_group="trigger")["count"] == 2
    assert TG.list_video_tags(niche="econ")["count"] == 2
    assert TG.list_video_tags(niche="econ", tag_group="trigger")["count"] == 1
    assert TG.list_video_tags(niche="nothing-here")["count"] == 0


def _stub_niche_videos(videos):
    global NICHE_VIDEOS
    NICHE_VIDEOS = {"videos": videos, "outlierBase": "rolling", "hint": None}
    fake_search.niche_videos = lambda **kw: dict(NICHE_VIDEOS, _called_with=kw)


def _video(vid, views, score, fresh=False):
    return {"videoId": vid, "title": f"video {vid}", "views": views,
            "outlierScore": score, "isFresh": fresh}


def test_tag_stats_joins_videos_with_their_tags():
    reset()
    _stub_niche_videos([_video("vid1", 100_000, 4.0), _video("vid2", 90_000, 3.0),
                        _video("vid3", 1_000, 0.3), _video("vid4", 900, 0.2)])
    TG.tag_videos([{"video_id": v, "tag_group": "topic_group_econ", "tag": t}
                   for v, t in (("vid1", "a"), ("vid2", "a"), ("vid3", "a"),
                                ("vid4", "b"))])
    res = TG.tag_stats(niche="econ", tag_group="topic_group_econ")
    assert res["videos"] == 4 and res["hits"] == 2
    a = next(t for t in res["tags"] if t["tag"] == "a")
    assert (a["videos"], a["hits"]) == (3, 2)
    assert a["hitRate"] == round(2 / 3, 4)
    assert a["lift"] == round((2 / 3) / 0.5, 2)
    assert res["hint"] is None and res["quota"] == 0


def test_tag_stats_only_counts_the_requested_group():
    reset()
    _stub_niche_videos([_video("vid1", 100_000, 4.0)])
    TG.tag_videos([{"video_id": "vid1", "tag_group": "topic_group_econ", "tag": "a"},
                   {"video_id": "vid1", "tag_group": "trigger", "tag": "curiosity"}])
    res = TG.tag_stats(niche="econ", tag_group="trigger")
    assert [t["tag"] for t in res["tags"]] == ["curiosity"]


def test_tag_stats_passes_the_outlier_settings_down():
    reset()
    _stub_niche_videos([_video("vid1", 100_000, 2.5)])
    TG.tag_videos([ITEM])
    res = TG.tag_stats(niche="econ", tag_group="topic_group_econ",
                       outlier_base="period", outlier_threshold=3.0, period="30d")
    assert res["tags"][0]["hits"] == 0               # 2.5 is below 3.0 now
    assert res["outlierBase"] == "rolling"           # whatever niche_videos reported


def test_tag_stats_forwards_period_and_base_to_niche_videos():
    reset()
    seen = {}
    fake_search.niche_videos = lambda **kw: (seen.update(kw),
                                             {"videos": [], "outlierBase": kw["outlier_base"],
                                              "hint": "nothing collected"})[1]
    TG.tag_stats(niche="econ", tag_group="g", period="30d", outlier_base="period",
                 outlier_threshold=3.0)
    assert seen == {"niche": "econ", "period": "30d", "outlier_base": "period",
                    "outlier_threshold": 3.0}
    _stub_niche_videos([])


def test_tag_stats_hints_when_the_group_is_empty():
    reset()
    _stub_niche_videos([_video("vid1", 100_000, 4.0)])
    res = TG.tag_stats(niche="econ", tag_group="topic_group_typo")
    assert res["tags"] == [] and "list_video_tags" in res["hint"]


def test_tag_stats_hints_when_every_tagged_video_is_fresh():
    reset()
    _stub_niche_videos([_video("vid1", 1_000, 0.2, fresh=True)])
    TG.tag_videos([ITEM])
    res = TG.tag_stats(niche="econ", tag_group="topic_group_econ")
    assert res["tags"] == [] and "include_fresh=True" in res["hint"]
    with_fresh = TG.tag_stats(niche="econ", tag_group="topic_group_econ",
                              include_fresh=True)
    assert with_fresh["tags"][0]["videos"] == 1


def test_tag_stats_group_name_is_normalised_like_the_write_path():
    reset()
    _stub_niche_videos([_video("vid1", 100_000, 4.0)])
    TG.tag_videos([ITEM])
    assert TG.tag_stats(niche="econ", tag_group=" Topic_Group_Econ ")["tags"]


def test_tag_stats_requires_a_niche_and_a_group():
    assert _raises(TG.tag_stats, niche="", tag_group="g")
    assert _raises(TG.tag_stats, niche="econ", tag_group="")


def _run_all():
    ns = dict(globals())
    tests = [(name, fn) for name, fn in ns.items() if name.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # pragma: no cover
            print(f"ERROR {name}: {e!r}")
    print(f"\n{passed}/{len(tests)} прошло")
    sys.exit(0 if passed == len(tests) else 1)  # иначе CI зеленеет при упавших тестах


if __name__ == "__main__":
    _run_all()
