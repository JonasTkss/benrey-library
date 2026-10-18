"""OpenRouter client with a model fallback chain.

OpenRouter exposes many models behind one OpenAI-compatible endpoint. Free
models are rate limited and come and go, so OPENROUTER_MODELS is an ordered
list: on a 429, a 5xx, a timeout or an unusable reply we move to the next
model. The whole call has a hard time budget so a slow provider cannot hang
an admin page's request.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 12.0
TOTAL_BUDGET = 20.0  # seconds across all models and retries
RETRY_BACKOFF = 0.5
MAX_ANSWER_CHARS = 1200
# Reasoning models spend part of this on hidden thinking before the answer, so
# it is well above what an 80-word answer needs. Free models cost nothing per
# token; the cap only bounds latency.
MAX_TOKENS = 800


class AssistantUnavailable(Exception):
    """No model produced a usable answer (or the assistant is not configured)."""


@dataclass
class ModelReply:
    content: str
    model: str


def is_configured() -> bool:
    return bool(settings.OPENROUTER_API_KEY and settings.OPENROUTER_MODELS)


REPLY_CACHE_SECONDS = 600


def complete(messages: list[dict], *, http: httpx.Client | None = None) -> ModelReply:
    """Return the first usable reply from the fallback chain.

    Identical requests (same prompt, same question, same earlier turns) within
    10 minutes reuse the stored reply: no second paid call, instant answer.
    The cached value is the model's own output (a lookup choice or a field
    explanation), never library data, because that is all it ever produces."""
    if not is_configured():
        raise AssistantUnavailable("not configured")
    payload = json.dumps([settings.OPENROUTER_MODELS, messages], sort_keys=True)
    key = "ai:" + hashlib.sha256(payload.encode()).hexdigest()
    hit = cache.get(key)
    if hit:
        return ModelReply(**hit)
    reply = _complete_uncached(messages, http)
    cache.set(key, {"content": reply.content, "model": reply.model}, REPLY_CACHE_SECONDS)
    return reply


def _complete_uncached(messages, http):
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "X-Title": "Benrey Library admin assistant",
    }
    deadline = time.monotonic() + TOTAL_BUDGET
    own_client = http is None
    http = http or httpx.Client()
    try:
        for model in settings.OPENROUTER_MODELS:
            for attempt in (1, 2):  # one retry per model for 429/5xx
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    raise AssistantUnavailable("time budget used up")
                outcome = _call(http, model, messages, headers, remaining)
                if isinstance(outcome, ModelReply):
                    return outcome
                if outcome == "retry" and attempt == 1:
                    time.sleep(RETRY_BACKOFF)
                    continue
                break  # next model
    finally:
        if own_client:
            http.close()
    raise AssistantUnavailable("all models failed")


def _call(http, model, messages, headers, remaining) -> ModelReply | str:
    """One request. Returns a reply, "retry" or "skip". Never raises for
    provider problems; those are logged and turned into a fallback step."""
    timeout = httpx.Timeout(min(READ_TIMEOUT, remaining), connect=CONNECT_TIMEOUT)
    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": MAX_TOKENS}
    started = time.monotonic()
    try:
        response = http.post(API_URL, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        _log(model, started, "timeout")
        return "skip"
    except httpx.HTTPError as exc:
        _log(model, started, f"network error {type(exc).__name__}")
        return "skip"

    status = response.status_code
    if status == 429 or status >= 500:
        _log(model, started, f"{status // 100}xx ({status})")
        return "retry"
    if status != 200:
        _log(model, started, f"{status // 100}xx ({status})")
        return "skip"
    try:
        choice = response.json()["choices"][0]
        content = choice["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        _log(model, started, "malformed body")
        return "skip"
    if choice.get("finish_reason") == "length":
        # Ran out of tokens mid-answer: a half sentence is worse than the next model.
        _log(model, started, "cut off (length)")
        return "skip"
    if not content.strip():
        _log(model, started, "empty reply")
        return "skip"
    _log(model, started, "ok")
    return ModelReply(content=content, model=model)


def _log(model, started, outcome):
    # Model, latency and outcome only: never the key, headers or user text.
    logger.info(
        "assistant model=%s outcome=%s latency_ms=%d",
        model,
        outcome,
        (time.monotonic() - started) * 1000,
    )


def parse_reply(content: str, known_ids: set[str]) -> tuple[str, list[str]]:
    """Read {"answer": ..., "sources": [...]} out of a model reply.

    Models sometimes wrap JSON in code fences or add prose, so take the first
    {...} block. If there is none, use the raw text with no sources. Any
    source id that is not a real field is dropped: the model cannot cite a
    field that does not exist.
    """
    answer, sources = content.strip(), []
    match = re.search(r"\{.*\}", content, re.DOTALL)
    data = None
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
    if isinstance(data, dict) and isinstance(data.get("answer"), str):
        answer = data["answer"].strip()
        raw_sources = data.get("sources")
        if isinstance(raw_sources, list):
            sources = [s for s in raw_sources if isinstance(s, str) and s in known_ids]
    else:
        # Broken or cut-off JSON (a free model hit its token limit mid-reply):
        # salvage the answer text instead of showing raw JSON to the user.
        partial = PARTIAL_ANSWER.search(content)
        if partial:
            try:
                answer = json.loads(f'"{partial.group(1)}"').strip()
            except ValueError:
                answer = partial.group(1).strip()
            sources = [s for s in re.findall(r"[a-z]+\.[a-z_]+", content) if s in known_ids]
    return answer[:MAX_ANSWER_CHARS], list(dict.fromkeys(sources))


def parse_lookup(content: str) -> tuple[str, dict] | None:
    """If the model asked for a data lookup ({"lookup": name, "params": {...}}),
    return (name, params). The name is checked against the allow-list later."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("lookup"), str):
        return None
    params = data.get("params")
    return data["lookup"], params if isinstance(params, dict) else {}


# The value of "answer" up to its closing quote, or to the end if it was cut off.
PARTIAL_ANSWER = re.compile(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', re.DOTALL)
