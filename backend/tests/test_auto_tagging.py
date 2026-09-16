"""Tests for application/auto_tagging.py (issue #7).

No network and no Postgres: the OpenRouter adapter, search.niche_videos and
the tag repository are stubbed. What is under test is everything that decides
what the model is asked and what is allowed back out of it -- the closed
taxonomy, the "add, never overwrite" rule, the budget, the newest-first order.

Run: python3 tests/test_auto_tagging.py (or pytest tests/test_auto_tagging.py)
"""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TAG_ROWS = []          # rows already in video_tags
VIDEOS = []            # what search.niche_videos returns
CALLS = []             # every chat_json call the use case made
ANSWERS = []           # queued model answers
WRITTEN = []           # what tagging.tag_videos was handed


class _Conn:
    def close(self):
        pass


fake_db = types.ModuleType("infrastructure.postgres")
fake_db.get_conn = lambda: _Conn()
fake_db.video_tags_for = lambda conn, video_ids=None, niche=None, tag_group=None: [
    r for r in TAG_ROWS if tag_group is None or r["tag_group"] == tag_group]
sys.modules["infrastructure.postgres"] = fake_db

fake_search = types.ModuleType("application.search")
fake_search.niche_videos = lambda **kw: {"videos": list(VIDEOS), "hint": None}
sys.modules["application.search"] = fake_search

fake_tagging = types.ModuleType("application.tagging")


def _tag_videos(items, source="claude-mcp", replace=False):
    WRITTEN.append({"items": items, "source": source, "replace": replace})
    return {"written": len(items), "skipped": 0, "removed": 0}


fake_tagging.tag_videos = _tag_videos
sys.modules["application.tagging"] = fake_tagging


class FakeLLMError(RuntimeError):
    pass


fake_llm = types.ModuleType("infrastructure.llm.openrouter")
fake_llm.LLMError = FakeLLMError
fake_llm.LLMNotConfigured = type("LLMNotConfigured", (FakeLLMError,), {})
fake_llm.api_key = lambda: os.environ.get("OPENROUTER_API_KEY", "")
fake_llm.model_name = lambda: os.environ.get("LLM_MODEL") or "z-ai/glm-5.3-flash"


def _chat_json(messages, schema, schema_name="result", model=None, **kw):
    CALLS.append({"messages": messages, "schema": schema, "model": model})
    answer = ANSWERS.pop(0) if ANSWERS else {"items": []}
    if isinstance(answer, Exception):
        raise answer
    return {"data": answer, "model": model or "z-ai/glm-5.3-flash",
            "usage": {"promptTokens": 100, "completionTokens": 20, "totalTokens": 120},
            "costUsd": 0.001}


fake_llm.chat_json = _chat_json
fake_llm_pkg = types.ModuleType("infrastructure.llm")
fake_llm_pkg.openrouter = fake_llm
sys.modules["infrastructure.llm"] = fake_llm_pkg
sys.modules["infrastructure.llm.openrouter"] = fake_llm

from application import auto_tagging as AT  # noqa: E402

REF = datetime(2026, 9, 16, tzinfo=timezone.utc)
GROUP = "topic_group_econ"


def video(video_id, title, age_days=100.0):
    return {"videoId": video_id, "title": title,
            "publishedAt": (REF - timedelta(days=age_days)).isoformat()}


def tag_row(video_id, tag, source="manual", group=GROUP):
    return {"video_id": video_id, "tag_group": group, "tag": tag, "source": source}


def setup(videos=None, tags=None, answers=None):
    global VIDEOS, TAG_ROWS
    VIDEOS = videos or []
    TAG_ROWS = tags or []
    ANSWERS.clear()
    ANSWERS.extend(answers or [])
    CALLS.clear()
    WRITTEN.clear()
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ.pop("LLM_TAGGING", None)


def taxonomy_of(n=6):
    """n videos already tagged by hand -- enough to clear min_taxonomy."""
    tags = []
    for i in range(n):
        tags.append(tag_row(f"old{i}", "a" if i % 2 else "b"))
    return tags


def test_a_group_without_enough_manual_tags_is_left_alone():
    setup(videos=[video("v1", "Owning a car wash")], tags=[tag_row("old0", "a")])
    res = AT.tag_new_videos("econ", GROUP, min_taxonomy=5)
    assert res["labelled"] == 0 and not CALLS
    assert "tag at least 5" in res["hint"]


def test_the_prompt_carries_the_taxonomy_the_examples_and_the_candidates():
    setup(videos=[video("old0", "Owning a casino"), video("v1", "Owning a car wash")],
          tags=taxonomy_of(), answers=[{"items": [{"video_id": "v1", "tags": ["a"]}]}])
    AT.tag_new_videos("econ", GROUP)
    prompt = CALLS[0]["messages"][1]["content"]
    assert GROUP in prompt
    assert "- a" in prompt and "- b" in prompt          # таксономия
    assert "Owning a casino" in prompt                  # пример разметки человеком
    assert 'v1  "Owning a car wash"' in prompt          # кандидат
    assert "old0  " not in prompt.split("Label these videos")[1]  # уже размечен


def test_the_schema_closes_the_taxonomy_to_existing_tags():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(),
          answers=[{"items": [{"video_id": "v1", "tags": ["a"]}]}])
    AT.tag_new_videos("econ", GROUP)
    schema = CALLS[0]["schema"]
    enum = schema["properties"]["items"]["items"]["properties"]["tags"]["items"]["enum"]
    assert sorted(enum) == ["a", "b"]
    assert schema["additionalProperties"] is False


def test_an_invented_tag_is_dropped_even_if_the_model_returns_it():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(),
          answers=[{"items": [{"video_id": "v1", "tags": ["a", "totally-new-tag"]}]}])
    res = AT.tag_new_videos("econ", GROUP)
    assert [i["tag"] for i in WRITTEN[0]["items"]] == ["a"]
    assert res["labelled"] == 1


def test_a_video_that_was_not_in_the_batch_is_ignored():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(),
          answers=[{"items": [{"video_id": "somebody-elses-video", "tags": ["a"]}]}])
    res = AT.tag_new_videos("econ", GROUP)
    assert res["labelled"] == 0 and not WRITTEN


def test_an_empty_answer_is_recorded_not_forced_into_a_tag():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(),
          answers=[{"items": [{"video_id": "v1", "tags": []}]}])
    res = AT.tag_new_videos("econ", GROUP)
    assert res["emptyAnswers"] == 1 and res["labelled"] == 0
    assert not WRITTEN


def test_tags_are_written_as_llm_and_never_replace():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(),
          answers=[{"items": [{"video_id": "v1", "tags": ["a"]}]}])
    AT.tag_new_videos("econ", GROUP)
    assert WRITTEN[0]["source"] == "llm"
    assert WRITTEN[0]["replace"] is False


def test_already_tagged_videos_are_not_candidates():
    setup(videos=[video("old0", "x"), video("v1", "y")], tags=taxonomy_of(),
          answers=[{"items": []}])
    res = AT.tag_new_videos("econ", GROUP)
    assert res["candidates"] == 1


def test_newest_candidates_go_first_when_the_limit_cuts_the_run():
    setup(videos=[video("vold", "old one", age_days=900.0),
                  video("vnew", "new one", age_days=2.0)],
          tags=taxonomy_of(), answers=[{"items": []}])
    res = AT.tag_new_videos("econ", GROUP, limit=1)
    assert res["candidates"] == 1
    assert "vnew" in CALLS[0]["messages"][1]["content"]
    assert "vold" not in CALLS[0]["messages"][1]["content"]


def test_candidates_are_split_into_batches():
    setup(videos=[video(f"v{i}", f"title {i}") for i in range(5)],
          tags=taxonomy_of(),
          answers=[{"items": []}, {"items": []}, {"items": []}])
    res = AT.tag_new_videos("econ", GROUP, batch_size=2)
    assert res["batches"] == 3 and len(CALLS) == 3


def test_cost_and_tokens_accumulate_across_batches():
    setup(videos=[video(f"v{i}", f"t{i}") for i in range(4)], tags=taxonomy_of(),
          answers=[{"items": []}, {"items": []}])
    res = AT.tag_new_videos("econ", GROUP, batch_size=2)
    assert res["costUsd"] == 0.002
    assert res["usage"] == {"promptTokens": 200, "completionTokens": 40}


def test_the_budget_stops_the_run_and_says_so():
    setup(videos=[video(f"v{i}", f"t{i}") for i in range(10)], tags=taxonomy_of(),
          answers=[{"items": []}] * 5)
    res = AT.tag_new_videos("econ", GROUP, batch_size=2, max_cost_usd=0.0015)
    assert res["batches"] == 2            # второй вызов перешагнул потолок
    assert "budget" in res["hint"]


def test_dry_run_asks_but_writes_nothing():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(),
          answers=[{"items": [{"video_id": "v1", "tags": ["a"]}]}])
    res = AT.tag_new_videos("econ", GROUP, dry_run=True)
    assert res["labelled"] == 1 and res["written"] == 0
    assert not WRITTEN and CALLS


def test_max_tags_per_video_is_enforced_locally_too():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(),
          answers=[{"items": [{"video_id": "v1", "tags": ["a", "b"]}]}])
    AT.tag_new_videos("econ", GROUP, max_tags_per_video=1)
    assert len(WRITTEN[0]["items"]) == 1


def test_tag_niche_walks_every_existing_group_but_invents_none():
    setup(videos=[video("v1", "x")],
          tags=taxonomy_of() + [tag_row("old0", "t1", group="triggers"),
                                tag_row("old1", "t2", group="triggers"),
                                tag_row("old2", "t1", group="triggers"),
                                tag_row("old3", "t2", group="triggers"),
                                tag_row("old4", "t1", group="triggers")],
          answers=[{"items": []}, {"items": []}])
    res = AT.tag_niche("econ")
    assert sorted(g["group"] for g in res["groups"]) == [GROUP, "triggers"]

    setup(videos=[video("v1", "x")], tags=[])
    empty = AT.tag_niche("econ")
    assert empty["groups"] == [] and "does not invent one" in empty["hint"]


def test_enabled_is_off_unless_the_flag_and_the_key_are_both_there():
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ.pop("LLM_TAGGING", None)
    assert AT.enabled() is False
    os.environ["LLM_TAGGING"] = "1"
    assert AT.enabled() is True
    os.environ["OPENROUTER_API_KEY"] = ""
    assert AT.enabled() is False
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["LLM_TAGGING"] = "0"
    assert AT.enabled() is False
    os.environ.pop("LLM_TAGGING")


def test_niche_and_group_are_required():
    setup()
    for args in (("", GROUP), ("econ", "")):
        try:
            AT.tag_new_videos(*args)
        except ValueError:
            continue
        raise AssertionError(f"{args} should be rejected")


def test_a_flat_map_answer_is_accepted_as_well_as_the_declared_shape():
    """Модель вернула {video_id: [tags]} вместо {"items": [...]} -- читаемо и
    отвергать это незачем, теги всё равно проверяются по таксономии."""
    setup(videos=[video("v1", "x"), video("v2", "y")], tags=taxonomy_of(),
          answers=[{"v1": ["a"], "v2": ["b"]}])
    res = AT.tag_new_videos("econ", GROUP)
    assert res["labelled"] == 2
    assert sorted((i["video_id"], i["tag"]) for i in WRITTEN[0]["items"]) == [
        ("v1", "a"), ("v2", "b")]


def test_the_prompt_shows_the_answer_shape_because_the_schema_is_only_a_hint():
    setup(videos=[video("v1", "x")], tags=taxonomy_of(), answers=[{"items": []}])
    AT.tag_new_videos("econ", GROUP)
    assert '{"items": [{"video_id"' in CALLS[0]["messages"][1]["content"]


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
