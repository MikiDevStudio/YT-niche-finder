"""OpenRouter chat completions, for the automatic tagging of issue #7.

The only outbound call in this project that is not YouTube, and the only one
that costs real money, so two things are non-negotiable here: the answer comes
back as JSON that matches a schema we declared, and every call reports what it
cost.

Checked against the OpenRouter docs on 2026-09-16:

  * POST https://openrouter.ai/api/v1/chat/completions
  * strict JSON is `response_format={"type": "json_schema", "json_schema":
    {"name", "strict": true, "schema"}}`. Support is per PROVIDER, not just per
    model -- the same model served by two providers may honour the schema on
    one and treat it as a hint on the other -- so every request also sends
    `provider={"require_parameters": true}`, which routes only to endpoints
    that actually implement the parameters we passed.
  * usage accounting is automatic now: `usage.cost` (USD charged),
    `usage.cost_details.upstream_inference_cost`, `prompt_tokens`,
    `completion_tokens`. The old `usage={"include": true}` request field is
    deprecated and does nothing.

The API key never reaches a log or an exception message: _redact strips it
from anything this module raises.
"""
import json
import os
import re
import time

import requests

BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_TIMEOUT = 120
RETRY_STATUS = (408, 429, 500, 502, 503, 504)

# Sent as attribution; OpenRouter shows them on the account's activity page,
# which is the only way to tell this project's spend from anything else's.
APP_URL = "https://github.com/MikiDevStudio/YT-niche-finder"
APP_TITLE = "niche-finder"


class LLMError(RuntimeError):
    """Anything that went wrong with the LLM call, key already redacted."""


class LLMNotConfigured(LLMError):
    """No API key. A missing key is a configuration state, not a failure --
    the worker skips tagging instead of retrying it every cycle."""


def api_key() -> str:
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def model_name() -> str:
    return (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL


def _redact(text: str) -> str:
    key = api_key()
    out = str(text)
    if key:
        out = out.replace(key, "<OPENROUTER_KEY>")
    return out


def chat_json(messages, schema, schema_name: str = "result", model: str = None,
              max_tokens: int = 2000, temperature: float = 0.0,
              timeout: int = DEFAULT_TIMEOUT, retries: int = 3,
              key: str = None, reasoning_effort: str = None) -> dict:
    """One chat completion that must answer with JSON matching `schema`.

    Returns {"data": parsed JSON, "model": str, "usage": {...}, "costUsd": float}.
    Raises LLMNotConfigured without a key, LLMError for everything else --
    including a reply that is not valid JSON, because a caller that writes tags
    into the database must not be handed a string that only looks like an answer.

    `reasoning_effort` ("minimal" | "low" | "medium" | "high") is worth setting
    on a reasoning model doing a mechanical job. Measured on z-ai/glm-5.3-flash,
    two videos to classify: default 153 reasoning tokens and $7.5e-05, "low" 27
    and $3.9e-05, "minimal" 12 and $3.1e-05. It is only sent when asked for,
    because it travels next to provider.require_parameters and would otherwise
    narrow routing for models that have no reasoning at all. Note that this
    model REJECTS effort "none": reasoning is mandatory on its endpoint.
    """
    key = (key or api_key())
    if not key:
        raise LLMNotConfigured(
            "OPENROUTER_API_KEY is not set -- put it in .env next to "
            "docker-compose.yml. Automatic tagging is off without it.")

    payload = {
        "model": model or model_name(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        # Only route to endpoints that honour response_format; otherwise a
        # provider is free to treat the schema as a suggestion.
        "provider": {"require_parameters": True},
    }
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_URL,
        "X-Title": APP_TITLE,
    }

    last = None
    for attempt in range(retries):
        try:
            resp = requests.post(f"{BASE}/chat/completions", headers=headers,
                                 json=payload, timeout=timeout)
        except requests.RequestException as e:
            last = None
            if attempt == retries - 1:
                raise LLMError(f"OpenRouter unreachable: {_redact(e)}")
            time.sleep(1.5 * (attempt + 1))
            continue

        if resp.status_code == 200:
            return _shape(resp.json())
        last = resp
        if resp.status_code in RETRY_STATUS and attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
            continue
        break

    detail = _redact(last.text)[:500] if last is not None else "no response"
    raise LLMError(f"OpenRouter error {last.status_code if last else '?'}: {detail}")


FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json(content):
    """The reply as JSON, tolerating a Markdown fence around it.

    Observed on 2026-09-16 with z-ai/glm-5.3-flash, which the catalogue lists
    as supporting `structured_outputs` and which was asked with strict mode and
    `require_parameters`: the answer still came back wrapped in ```json. The
    docs say as much -- some providers "treat [the schema] as a strong hint" --
    so the schema is a steering device here, never a guarantee, and the caller
    still has to validate what it gets.
    """
    if not isinstance(content, str):
        raise LLMError(f"model returned {type(content).__name__}, not text")
    text = content.strip()
    fenced = FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except (TypeError, ValueError) as e:
        raise LLMError(f"model did not answer with JSON ({e}): {text[:300]}")


def _shape(body: dict) -> dict:
    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"OpenRouter returned no choices: {_redact(json.dumps(body))[:300]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    finish = choices[0].get("finish_reason")
    if not content:
        raise LLMError(f"OpenRouter returned an empty message (finish_reason={finish})")
    data = parse_json(content)

    usage = body.get("usage") or {}
    return {
        "data": data,
        "model": body.get("model"),
        "finishReason": finish,
        "usage": {
            "promptTokens": usage.get("prompt_tokens"),
            "completionTokens": usage.get("completion_tokens"),
            "totalTokens": usage.get("total_tokens"),
        },
        "costUsd": float(usage.get("cost") or 0.0),
    }
