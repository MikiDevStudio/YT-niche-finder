"""Tests for infrastructure/llm/openrouter.py (issue #7).

No network: requests.post is replaced. What is under test is the contract with
OpenRouter as its docs describe it -- strict JSON schema, provider routing that
requires the parameters, cost read off the usage object -- and, just as
importantly, that the API key never leaves this module inside an error message.

Run: python3 tests/test_openrouter.py (or pytest tests/test_openrouter.py)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import requests  # noqa: E402
from infrastructure.llm import openrouter as LLM  # noqa: E402

KEY = "sk-or-v1-secret-key-value"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}

SENT = []


class _Resp:
    def __init__(self, status_code, body=None, text=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = text if text is not None else json.dumps(self._body)

    def json(self):
        return self._body


def _ok_body(content='{"ok": true}', cost=0.000123):
    return {
        "id": "gen-1", "model": "z-ai/glm-5.3-flash",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128,
                  "cost": cost, "cost_details": {"upstream_inference_cost": 0.0001}},
    }


def fake_post(responses):
    """Queue of responses (or exceptions) for consecutive calls."""
    queue = list(responses)
    SENT.clear()

    def post(url, headers=None, json=None, timeout=None):
        SENT.append({"url": url, "headers": headers, "body": json, "timeout": timeout})
        item = queue.pop(0) if queue else _Resp(200, _ok_body())
        if isinstance(item, Exception):
            raise item
        return item
    requests.post = post


_real_post = requests.post


def setup():
    os.environ["OPENROUTER_API_KEY"] = KEY
    os.environ.pop("LLM_MODEL", None)


def call(**kw):
    return LLM.chat_json([{"role": "user", "content": "hi"}], SCHEMA, **kw)


def test_request_asks_for_strict_json_and_a_provider_that_honours_it():
    setup()
    fake_post([_Resp(200, _ok_body())])
    res = call(schema_name="video_tags")

    body = SENT[0]["body"]
    assert SENT[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "video_tags"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    # без require_parameters провайдер вправе считать схему пожеланием
    assert body["provider"]["require_parameters"] is True
    assert res["data"] == {"ok": True}


def test_key_travels_in_the_header_with_attribution():
    setup()
    fake_post([_Resp(200, _ok_body())])
    call()
    headers = SENT[0]["headers"]
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert headers["X-Title"] and headers["HTTP-Referer"]


def test_cost_and_tokens_come_back_from_the_usage_object():
    setup()
    fake_post([_Resp(200, _ok_body(cost=0.00042))])
    res = call()
    assert res["costUsd"] == 0.00042
    assert res["usage"] == {"promptTokens": 120, "completionTokens": 8, "totalTokens": 128}
    assert res["model"] == "z-ai/glm-5.3-flash"


def test_a_missing_cost_is_zero_not_a_crash():
    setup()
    body = _ok_body()
    body["usage"].pop("cost")
    fake_post([_Resp(200, body)])
    assert call()["costUsd"] == 0.0


def test_default_model_is_used_unless_llm_model_is_set():
    setup()
    fake_post([_Resp(200, _ok_body()), _Resp(200, _ok_body())])
    call()
    assert SENT[0]["body"]["model"] == LLM.DEFAULT_MODEL
    os.environ["LLM_MODEL"] = "z-ai/glm-5.3"
    call()
    assert SENT[1]["body"]["model"] == "z-ai/glm-5.3"
    os.environ.pop("LLM_MODEL")


def test_no_key_is_a_configuration_state_not_a_failure():
    os.environ.pop("OPENROUTER_API_KEY", None)
    fake_post([_Resp(200, _ok_body())])
    try:
        call()
    except LLM.LLMNotConfigured as e:
        assert "OPENROUTER_API_KEY" in str(e)
    else:
        raise AssertionError("a missing key should raise LLMNotConfigured")
    finally:
        os.environ["OPENROUTER_API_KEY"] = KEY


def test_an_answer_that_is_not_json_is_an_error_not_a_string():
    setup()
    fake_post([_Resp(200, _ok_body(content="Sure! Here are the tags:"))])
    try:
        call()
    except LLM.LLMError as e:
        assert "did not answer with JSON" in str(e)
    else:
        raise AssertionError("prose should not reach the caller as an answer")


def test_an_empty_message_is_an_error():
    setup()
    body = _ok_body()
    body["choices"][0]["message"]["content"] = ""
    body["choices"][0]["finish_reason"] = "length"
    fake_post([_Resp(200, body)])
    try:
        call()
    except LLM.LLMError as e:
        assert "length" in str(e)
    else:
        raise AssertionError("an empty message should raise")


def test_a_retryable_status_is_retried_then_succeeds():
    setup()
    fake_post([_Resp(429, text="rate limited"), _Resp(200, _ok_body())])
    assert call(retries=2)["data"] == {"ok": True}
    assert len(SENT) == 2


def test_a_permanent_error_is_not_retried():
    setup()
    fake_post([_Resp(400, text="bad request"), _Resp(200, _ok_body())])
    try:
        call(retries=3)
    except LLM.LLMError as e:
        assert "400" in str(e)
    else:
        raise AssertionError("a 400 should not be retried into a success")
    assert len(SENT) == 1


def test_the_key_is_never_in_an_error_message():
    setup()
    fake_post([_Resp(401, text=f"invalid key {KEY} rejected")])
    try:
        call(retries=1)
    except LLM.LLMError as e:
        assert KEY not in str(e)
        assert "<OPENROUTER_KEY>" in str(e)
    else:
        raise AssertionError("a 401 should raise")


def test_a_network_failure_is_retried_and_then_reported():
    setup()
    fake_post([requests.RequestException("connection reset"),
               requests.RequestException("connection reset")])
    try:
        call(retries=2)
    except LLM.LLMError as e:
        assert "unreachable" in str(e)
    else:
        raise AssertionError("an unreachable API should raise")
    assert len(SENT) == 2


def test_a_markdown_fence_around_the_json_is_tolerated():
    """z-ai/glm-5.3-flash отвечает так, несмотря на strict-схему и
    require_parameters: схема здесь -- рекомендация, а не гарантия."""
    setup()
    fenced = "```json\n{\"ok\": true}\n```"
    fake_post([_Resp(200, _ok_body(content=fenced))])
    assert call()["data"] == {"ok": True}

    bare_fence = "```\n{\"ok\": false}\n```"
    fake_post([_Resp(200, _ok_body(content=bare_fence))])
    assert call()["data"] == {"ok": False}


def test_reasoning_effort_is_sent_only_when_asked_for():
    setup()
    fake_post([_Resp(200, _ok_body()), _Resp(200, _ok_body())])
    call()
    assert "reasoning" not in SENT[0]["body"]
    call(reasoning_effort="low")
    assert SENT[1]["body"]["reasoning"] == {"effort": "low"}


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
        finally:
            requests.post = _real_post
    print(f"\n{passed}/{len(tests)} прошло")
    sys.exit(0 if passed == len(tests) else 1)  # иначе CI зеленеет при упавших тестах


if __name__ == "__main__":
    _run_all()
