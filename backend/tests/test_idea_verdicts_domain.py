"""Tests for domain/idea_verdicts.py -- the four verdicts (issue #3).

Pure rule, no database and no clock: every row here is handed in with its age
already computed, which is exactly how application/ideas.py calls it.

Run: python3 tests/test_idea_verdicts_domain.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from domain import idea_verdicts as V  # noqa: E402


def m(video_id="v", age_days=400.0, views=10000, outlier=1.0):
    return {"video_id": video_id, "age_days": age_days, "views": views,
            "outlier": outlier}


def test_no_match_is_free():
    res = V.decide([])
    assert res["verdict"] == "free"
    assert res["matches"] == 0 and res["daysSinceLastCoverage"] is None


def test_covered_inside_the_window_is_recent_whatever_it_scored():
    """Свежий конкурент — это лобовое столкновение, как бы он ни выступил."""
    for score in (0.2, 9.0):
        res = V.decide([m(age_days=10.0, outlier=score)])
        assert res["verdict"] == "recent", score
        assert res["daysSinceLastCoverage"] == 10.0


def test_old_and_broke_out_is_proven():
    res = V.decide([m(age_days=400.0, outlier=3.4, video_id="hit")])
    assert res["verdict"] == "proven"
    assert res["bestOutlier"] == 3.4 and res["bestOutlierVideoId"] == "hit"


def test_old_and_flat_is_flopped():
    res = V.decide([m(age_days=400.0, outlier=0.6)])
    assert res["verdict"] == "flopped"
    assert "0.6" in res["why"]


def test_the_best_of_several_matches_decides():
    res = V.decide([m("a", 400.0, outlier=0.4), m("b", 500.0, outlier=2.8)])
    assert res["verdict"] == "proven" and res["bestOutlierVideoId"] == "b"
    assert res["matches"] == 2
    assert res["daysSinceLastCoverage"] == 400.0   # самое свежее из покрытий


def test_fresh_matches_do_not_get_to_call_it_a_flop():
    """Ролик моложе 30 дней ещё набирает просмотры. Здесь окно recent сужено,
    так что вердикт решает зрелая часть выборки, а не свежая."""
    res = V.decide([m("fresh", 5.0, outlier=0.3), m("old", 400.0, outlier=2.5)],
                   recent_days=1.0)
    assert res["verdict"] == "proven"
    assert res["freshMatches"] == 1


def test_all_matches_fresh_and_no_recent_window_judges_on_what_exists():
    res = V.decide([m("fresh", 5.0, outlier=4.0)], recent_days=1.0)
    assert res["verdict"] == "proven"


def test_old_coverage_without_a_score_is_flopped_but_says_why():
    res = V.decide([m(age_days=400.0, outlier=None)])
    assert res["verdict"] == "flopped"
    assert "no outlier score" in res["why"]
    assert res["bestOutlier"] is None


def test_thresholds_are_parameters():
    rows = [m(age_days=200.0, outlier=1.5)]
    assert V.decide(rows)["verdict"] == "flopped"
    assert V.decide(rows, proven_outlier=1.2)["verdict"] == "proven"
    assert V.decide(rows, recent_days=365.0)["verdict"] == "recent"


def test_every_verdict_is_one_of_the_documented_four():
    cases = [[], [m(age_days=1.0)], [m(age_days=400.0, outlier=5.0)],
             [m(age_days=400.0, outlier=0.1)]]
    assert {V.decide(c)["verdict"] for c in cases} == set(V.VERDICTS)


def test_max_views_counts_every_match_fresh_ones_too():
    res = V.decide([m("a", 400.0, views=1000, outlier=1.0),
                    m("b", 5.0, views=90000, outlier=1.0)], recent_days=1.0)
    assert res["maxViews"] == 90000


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
