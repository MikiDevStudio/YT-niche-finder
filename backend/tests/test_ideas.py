"""Tests for application/ideas.py -- check_ideas (issue #3).

No Postgres, no network and no embeddings model: discovery.load_window and the
embeddings adapter are stubbed, the way test_tagging.py stubs the layer below
it. What is under test is the matching -- which video counts as coverage of an
idea, and why -- while the verdict rule itself is covered in
test_idea_verdicts_domain.py.

Run: python3 tests/test_ideas.py (or pytest tests/test_ideas.py)
"""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

ROWS = []
VECTORS = {}          # video_id -> "vector" (any hashable stand-in)
SIMILARITY = {}       # (idea, video_id) -> cosine


class _Conn:
    def execute(self, sql, params=()):
        rows = [{"video_id": v, "embedding": v} for v in VECTORS if v in set(params)]
        return types.SimpleNamespace(fetchall=lambda: rows)

    def close(self):
        pass


fake_db = types.ModuleType("infrastructure.postgres")
fake_db.get_conn = lambda: _Conn()
sys.modules["infrastructure.postgres"] = fake_db

# The embeddings adapter, without fastembed: from_blob hands back the video id
# and cosine_matrix reads the similarities the test declared.
fake_emb = types.ModuleType("infrastructure.embeddings.fastembed_provider")
fake_emb.from_blob = lambda blob: blob
fake_emb.embed = lambda texts: list(texts)
fake_emb.cosine_matrix = lambda queries, vecs: [
    [SIMILARITY.get((qq, v), 0.0) for v in vecs] for qq in queries]
fake_emb_pkg = types.ModuleType("infrastructure.embeddings")
fake_emb_pkg.fastembed_provider = fake_emb
sys.modules["infrastructure.embeddings"] = fake_emb_pkg
sys.modules["infrastructure.embeddings.fastembed_provider"] = fake_emb

fake_trends = types.ModuleType("application.discovery")
fake_trends.load_window = lambda **kw: [dict(r) for r in ROWS]
sys.modules["application.discovery"] = fake_trends

from application import ideas as IDEAS  # noqa: E402


REF = datetime(2026, 9, 16, tzinfo=timezone.utc)


def row(video_id, title, age_days=400.0, views=10000, outlier=1.0):
    """published_at is derived from age_days so the two never disagree -- the
    verdict reads the age, the example list sorts on the date."""
    return {"video_id": video_id, "title": title, "channel_id": "UC1",
            "channel_title": "Millionaire Problems", "subs": 28600,
            "published_at": (REF - timedelta(days=age_days)).isoformat(),
            "ageDays": age_days, "view_count": views, "outlierScore": outlier,
            "outlierBase": "rolling"}


def setup(rows, vectors=None, similarity=None):
    global ROWS, VECTORS, SIMILARITY
    ROWS = rows
    VECTORS = {v: v for v in (vectors or [])}
    SIMILARITY = similarity or {}


def one(res, idea=None):
    if idea is None:
        return res["ideas"][0]
    return next(r for r in res["ideas"] if r["idea"] == idea)


def test_an_uncovered_idea_is_free():
    setup([row("v1", "The Economics of Owning a Casino")])
    res = IDEAS.check_ideas(["car wash"])
    assert one(res)["verdict"] == "free" and one(res)["matches"] == 0
    assert res["counts"]["free"] == 1


def test_a_phrase_in_the_title_is_a_match_case_and_punctuation_aside():
    setup([row("v1", "The Economics of Owning a CAR WASH!", age_days=400.0, outlier=3.0)])
    res = IDEAS.check_ideas(["car wash"])
    item = one(res)
    assert item["verdict"] == "proven"
    assert item["videos"][0]["matchedBy"] == "title"
    assert item["videos"][0]["videoId"] == "v1"


def test_a_title_match_is_whole_words_not_any_substring():
    setup([row("v1", "Supercarwashing machines explained")])
    assert one(IDEAS.check_ideas(["car wash"]))["verdict"] == "free"


def test_semantics_catch_what_the_title_does_not_say():
    setup([row("v1", "What it costs to run a crematorium", age_days=400.0, outlier=0.5)],
          vectors=["v1"], similarity={("funeral home", "v1"): 0.71})
    item = one(IDEAS.check_ideas(["funeral home"]))
    assert item["verdict"] == "flopped"
    assert item["videos"][0]["matchedBy"] == "semantic"
    assert item["videos"][0]["similarity"] == 0.71


def test_similarity_below_the_threshold_is_not_coverage():
    setup([row("v1", "Отель на берегу моря")],
          vectors=["v1"], similarity={("funeral home", "v1"): 0.4})
    assert one(IDEAS.check_ideas(["funeral home"]))["verdict"] == "free"
    assert one(IDEAS.check_ideas(["funeral home"], min_similarity=0.3))["verdict"] != "free"


def test_a_video_matched_both_ways_says_so():
    setup([row("v1", "Owning a car wash", age_days=5.0)],
          vectors=["v1"], similarity={("car wash", "v1"): 0.9})
    item = one(IDEAS.check_ideas(["car wash"]))
    assert item["verdict"] == "recent"
    assert item["videos"][0]["matchedBy"] == "both"


def test_match_titles_false_leaves_only_the_semantic_half():
    setup([row("v1", "Owning a car wash")])
    assert one(IDEAS.check_ideas(["car wash"], match_titles=False))["verdict"] == "free"


def test_examples_are_newest_first_and_capped():
    setup([row("v1", "car wash one", age_days=900.0),
           row("v2", "car wash two", age_days=400.0),
           row("v3", "car wash three", age_days=600.0)])
    item = one(IDEAS.check_ideas(["car wash"], examples=2))
    assert item["matches"] == 3
    assert [v["videoId"] for v in item["videos"]] == ["v2", "v3"]
    assert item["medianViews"] == 10000


def test_every_idea_gets_its_own_verdict_and_the_counts_add_up():
    setup([row("v1", "Owning a car wash", age_days=10.0),
           row("v2", "Owning a funeral home", age_days=800.0, outlier=4.0),
           row("v3", "Owning a laundromat", age_days=800.0, outlier=0.3)])
    res = IDEAS.check_ideas(["car wash", "funeral home", "laundromat", "toll booth"])
    assert one(res, "car wash")["verdict"] == "recent"
    assert one(res, "funeral home")["verdict"] == "proven"
    assert one(res, "laundromat")["verdict"] == "flopped"
    assert one(res, "toll booth")["verdict"] == "free"
    assert res["counts"] == {"free": 1, "recent": 1, "proven": 1, "flopped": 1}
    assert res["count"] == 4


def test_the_same_idea_twice_is_checked_once():
    setup([row("v1", "Owning a car wash")])
    res = IDEAS.check_ideas(["car wash", "Car  Wash!"])
    assert res["count"] == 1


def test_an_empty_corpus_says_free_is_not_an_answer():
    setup([])
    res = IDEAS.check_ideas(["car wash"])
    assert one(res)["verdict"] == "free"
    assert "corpus is empty" in res["hint"]


def test_a_corpus_without_embeddings_admits_the_semantic_half_did_not_run():
    setup([row("v1", "Owning a laundromat")])
    res = IDEAS.check_ideas(["car wash"])
    assert res["corpus"] == {"videos": 1, "embedded": 0}
    assert "backfill_embeddings" in res["hint"]


def test_partly_embedded_corpus_is_reported_too():
    setup([row("v1", "a"), row("v2", "b"), row("v3", "c")], vectors=["v1"])
    res = IDEAS.check_ideas(["car wash"])
    assert res["corpus"] == {"videos": 3, "embedded": 1}
    assert "only 1 of 3" in res["hint"]


def test_thresholds_travel_with_the_answer():
    setup([row("v1", "Owning a car wash", age_days=400.0, outlier=1.5)])
    res = IDEAS.check_ideas(["car wash"], proven_outlier=1.2, recent_days=30.0)
    assert res["thresholds"] == {"recentDays": 30.0, "provenOutlier": 1.2, "freshDays": 30.0}
    assert one(res)["verdict"] == "proven"


def test_bad_input_is_a_value_error():
    setup([])
    for bad in ([], "", [""], [123], None, ["ok", "  "]):
        try:
            IDEAS.check_ideas(bad)
        except ValueError:
            continue
        raise AssertionError(f"ideas={bad!r} should be rejected")
    try:
        IDEAS.check_ideas(["x"] * (IDEAS.MAX_IDEAS + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("too many ideas should be rejected")


def test_a_single_string_is_accepted_as_one_idea():
    setup([row("v1", "Owning a car wash")])
    assert IDEAS.check_ideas("car wash")["count"] == 1


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
