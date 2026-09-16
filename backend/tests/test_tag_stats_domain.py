"""Tests for domain/tag_stats.py -- pure arithmetic of the tag hit rate
(issue #2). No database, no key, no network.

Run: python3 tests/test_tag_stats_domain.py (or pytest tests/test_tag_stats_domain.py)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from domain import tag_stats as TS  # noqa: E402


def row(vid, views, outlier, fresh=False, title=None):
    return {"video_id": vid, "title": title or f"video {vid}", "views": views,
            "outlier": outlier, "fresh": fresh}


def seed():
    """Group 'topic': 'a' on 4 videos (3 of them outliers >= 2.0), 'b' on 2
    (none), one video untagged. Base rate: 3 hits / 7 videos."""
    rows = [row("v1", 100_000, 4.0), row("v2", 90_000, 3.0), row("v3", 80_000, 2.0),
            row("v4", 1_000, 0.4), row("v5", 5_000, 1.1), row("v6", 6_000, 0.9),
            row("v7", 7_000, 1.0)]
    tags = {"v1": ["a"], "v2": ["a"], "v3": ["a"], "v4": ["a"],
            "v5": ["b"], "v6": ["b"]}
    return rows, tags


def test_hit_rate_and_lift_against_the_whole_sample():
    rows, tags = seed()
    res = TS.compute(rows, tags)
    assert res["videos"] == 7 and res["hits"] == 3
    assert res["baseRate"] == round(3 / 7, 4)
    a = next(t for t in res["tags"] if t["tag"] == "a")
    assert (a["videos"], a["hits"]) == (4, 3)
    assert a["hitRate"] == 0.75
    assert a["lift"] == round(0.75 / (3 / 7), 2)  # 1.75
    b = next(t for t in res["tags"] if t["tag"] == "b")
    assert b["hits"] == 0 and b["hitRate"] == 0.0 and b["lift"] == 0.0


def test_untagged_videos_are_counted_but_not_tagged():
    rows, tags = seed()
    res = TS.compute(rows, tags)
    assert res["untagged"] == 1                      # v7 carries no tag
    assert sum(t["videos"] for t in res["tags"]) == 6


def test_fresh_videos_leave_both_sides_of_the_fraction():
    """A tag whose only flop is a 5-day-old video is not a failing tag: the
    views simply have not arrived yet."""
    rows = [row("v1", 100_000, 4.0), row("v2", 90_000, 3.0),
            row("v3", 500, 0.1, fresh=True)]
    tags = {"v1": ["a"], "v2": ["a"], "v3": ["a"]}
    res = TS.compute(rows, tags)
    assert res["freshExcluded"] == 1
    assert res["videos"] == 2
    assert res["tags"][0]["videos"] == 2 and res["tags"][0]["hitRate"] == 1.0

    with_fresh = TS.compute(rows, tags, include_fresh=True)
    assert with_fresh["fresh"] == 1                  # still reported...
    assert with_fresh["freshExcluded"] == 0          # ...but nothing was dropped
    assert with_fresh["videos"] == 3
    assert with_fresh["tags"][0]["hitRate"] == round(2 / 3, 4)


def test_threshold_moves_what_counts_as_a_hit():
    rows, tags = seed()
    strict = TS.compute(rows, tags, outlier_threshold=3.5)
    a = next(t for t in strict["tags"] if t["tag"] == "a")
    assert a["hits"] == 1                            # only v1 at 4.0 survives
    assert strict["outlierThreshold"] == 3.5


def test_lift_is_none_when_nothing_broke_out_at_all():
    """No baseline to divide by -- None, not a zero division dressed up as 0."""
    rows = [row("v1", 100, 0.5), row("v2", 200, 0.9)]
    res = TS.compute(rows, {"v1": ["a"], "v2": ["a"]})
    assert res["baseRate"] == 0.0
    assert res["tags"][0]["lift"] is None


def test_a_video_counts_once_per_tag_of_a_multi_valued_group():
    """Triggers come in threes: one video feeds three tags, and each of them
    sees the whole video, not a third of it."""
    rows = [row("v1", 100_000, 4.0), row("v2", 1_000, 0.5)]
    tags = {"v1": ["curiosity", "recognition", "fantasy"], "v2": ["curiosity"]}
    res = TS.compute(rows, tags)
    by_tag = {t["tag"]: t for t in res["tags"]}
    assert by_tag["curiosity"]["videos"] == 2 and by_tag["curiosity"]["hits"] == 1
    assert by_tag["fantasy"]["videos"] == 1 and by_tag["fantasy"]["hitRate"] == 1.0
    assert res["videos"] == 2                        # the sample is still two videos


def test_duplicate_tag_on_one_video_counts_once():
    rows = [row("v1", 100_000, 4.0)]
    res = TS.compute(rows, {"v1": ["a", "a"]})
    assert res["tags"][0]["videos"] == 1


def test_min_videos_drops_noisy_tags():
    rows, tags = seed()
    res = TS.compute(rows, tags, min_videos=3)
    assert [t["tag"] for t in res["tags"]] == ["a"]   # 'b' has only 2 videos


def test_medians_not_means():
    """One viral video must not drag the tag's median with it -- the same
    reason the whole service ranks on medians."""
    rows = [row("v1", 1_000, 1.0), row("v2", 2_000, 1.0), row("v3", 900_000, 30.0)]
    res = TS.compute(rows, {"v1": ["a"], "v2": ["a"], "v3": ["a"]})
    assert res["tags"][0]["medianViews"] == 2_000
    assert res["tags"][0]["medianOutlier"] == 1.0


def test_examples_are_the_biggest_hits_and_capped_at_three():
    rows = [row(f"v{i}", i * 1000, float(i)) for i in range(1, 6)]
    res = TS.compute(rows, {f"v{i}": ["a"] for i in range(1, 6)})
    assert [e["videoId"] for e in res["tags"][0]["examples"]] == ["v5", "v4", "v3"]


def test_sorting_options():
    rows, tags = seed()
    assert [t["tag"] for t in TS.compute(rows, tags)["tags"]] == ["a", "b"]
    by_views = TS.compute(rows, tags, sort_by="median_views")["tags"]
    assert by_views[0]["tag"] == "a"                 # 85k vs 5.5k
    by_count = TS.compute(rows, tags, sort_by="videos")["tags"]
    assert by_count[0]["videos"] == 4


def test_empty_input_does_not_divide_by_zero():
    res = TS.compute([], {})
    assert res == {"tags": [], "videos": 0, "hits": 0, "baseRate": 0.0,
                   "untagged": 0, "fresh": 0, "freshExcluded": 0,
                   "outlierThreshold": 2.0}


def test_missing_outlier_score_is_not_a_hit():
    """A video whose baseline could not be computed must not count as a
    breakout just because its score is None."""
    rows = [row("v1", 100_000, None), row("v2", 100_000, 4.0)]
    res = TS.compute(rows, {"v1": ["a"], "v2": ["a"]})
    assert res["hits"] == 1
    assert res["tags"][0]["medianOutlier"] == 4.0    # None is skipped, not zeroed


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
