"""Provider-agnostic LLM client (Ollama / Groq / Gemini via OpenAI-compatible endpoints).

The only public call is `chat_json(...)`: every LLM response is parsed as JSON and validated into a
Pydantic model (extra="forbid"). One corrective retry on invalid output, then `LLMError`.
Callers MUST treat LLMError as "fail safe" (pause for a human), never as "clear".
"""
from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src import config

T = TypeVar("T", bound=BaseModel)

# Where the current case collects its own AI usage. Each worker thread has its own context, so cases checked
# in parallel never mix their token counts.
USAGE_SINK: ContextVar[list | None] = ContextVar("usage_sink", default=None)


def record_usage(entry: dict) -> None:
    sink = USAGE_SINK.get()
    if sink is not None:
        sink.append(entry)


_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


MAX_RATE_LIMIT_WAIT_S = 20.0   # longer waits (e.g. daily quota) => stop calling the AI until then


def _retry_after_seconds(e) -> float:
    """Seconds the provider asks us to wait (Retry-After header, or 'try again in 10m1.3s' in the message)."""
    try:
        h = e.response.headers.get("retry-after")
        if h:
            return float(h)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", str(e))
    if m and any(m.groups()):
        h_, m_, s_ = (float(x) if x else 0.0 for x in m.groups())
        return h_ * 3600 + m_ * 60 + s_
    return 0.0


class LLMError(Exception):
    """LLM unavailable or returned unusable output after retry."""


def extract_json(raw: str) -> dict:
    text = _FENCE.sub("", _THINK.sub("", raw or "")).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON root is not an object")
    return data


class LLMClient:
    def __init__(self, base_url: str = config.LLM_BASE_URL, model: str = config.LLM_MODEL,
                 api_key: str = config.LLM_API_KEY, timeout: float = config.LLM_TIMEOUT_S,
                 max_tokens: int = config.LLM_MAX_TOKENS,
                 reasoning_effort: str = config.LLM_REASONING_EFFORT):
        from openai import OpenAI  # imported lazily so code-only paths/tests don't need it

        self.model = model
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self._json_mode = True
        self._client = OpenAI(base_url=base_url, api_key=api_key or "none", timeout=timeout, max_retries=0)
        self.usage_log: list[dict] = []
        self._blocked_until = 0.0     # set when the provider says "try again in N minutes"

    # ---- low level ----------------------------------------------------------------
    def _complete(self, messages: list[dict], purpose: str) -> str:
        import openai

        kwargs: dict = dict(model=self.model, messages=messages, temperature=0, max_tokens=self.max_tokens)
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        wait = self._blocked_until - time.time()
        if wait > 0:   # quota exhausted earlier: don't hammer the API, fail fast so the case falls back to code
            raise LLMError(f"AI quota exhausted; retry in {wait / 60:.0f} min")
        for attempt in range(4):
            t0 = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except openai.RateLimitError as e:
                retry_after = _retry_after_seconds(e)
                if retry_after > MAX_RATE_LIMIT_WAIT_S:   # e.g. daily token quota: waiting won't help this batch
                    self._blocked_until = time.time() + retry_after
                    raise LLMError(f"rate limited for {retry_after / 60:.0f} min: {e}") from e
                if attempt == 3:
                    raise LLMError(f"rate limited: {e}") from e
                time.sleep(max(retry_after, 2 ** (attempt + 1)))
                continue
            except openai.BadRequestError as e:
                msg = str(e).lower()
                if "response_format" in kwargs and ("response_format" in msg or "json" in msg):
                    kwargs.pop("response_format")
                    self._json_mode = False
                    continue
                if "reasoning_effort" in kwargs and "reasoning" in msg:
                    kwargs.pop("reasoning_effort")
                    self.reasoning_effort = ""
                    continue
                raise LLMError(f"bad request: {e}") from e
            except (openai.APIConnectionError, openai.APITimeoutError, openai.APIStatusError) as e:
                raise LLMError(f"{type(e).__name__}: {e}") from e
            u = resp.usage
            entry = dict(purpose=purpose, model=self.model, latency_s=round(time.perf_counter() - t0, 3),
                         prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                         completion_tokens=getattr(u, "completion_tokens", 0) or 0)
            self.usage_log.append(entry)
            record_usage(entry)
            return resp.choices[0].message.content or ""
        raise LLMError("exhausted retries")

    # ---- public -------------------------------------------------------------------
    def chat_json(self, system: str, user: str, model_cls: type[T], *, purpose: str = "") -> T:
        if self.model.startswith("qwen3"):
            user += "\n/no_think"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_err: Exception | None = None
        for _ in range(2):
            raw = self._complete(messages, purpose)
            try:
                return model_cls.model_validate(extract_json(raw))
            except (ValueError, ValidationError) as e:
                last_err = e
                messages = messages + [
                    {"role": "assistant", "content": raw[:4000]},
                    {"role": "user", "content": f"Invalid output: {str(e)[:500]}. Reply again with ONLY "
                                                f"one JSON object matching the required schema."},
                ]
        raise LLMError(f"invalid {model_cls.__name__} after retry: {last_err}")


@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    return LLMClient()
