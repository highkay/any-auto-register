"""Sync HTTP vision LLM query + multi-model majority vote.

Reads keys from config_store / env (vision_api_base, vision_api_key, vision_model).
No hard-coded secrets.
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from typing import Any, Callable

import requests

InterruptChecker = Callable[[], None] | None

_REFUSAL_MARKERS = (
    "cannot fulfill",
    "can't fulfill",
    "cannot assist",
    "can't assist",
    "i am unable",
    "i'm unable",
    "safety guidelines",
    "not able to help",
    "cannot help with that",
)


def _cfg(name: str, default: str = "") -> str:
    env = os.getenv(name.upper()) or os.getenv(name)
    if env:
        return str(env).strip()
    try:
        from core.config_store import config_store

        val = config_store.get(name, "") or config_store.get(name.lower(), "")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return default


def looks_like_refusal(text: str | None) -> bool:
    if not text:
        return True
    t = text.lower()
    return any(m in t for m in _REFUSAL_MARKERS)


def _load_keys() -> list[str]:
    keys: list[str] = []
    for name in ("vision_api_key", "VISION_API_KEY", "OPENAI_API_KEY"):
        v = _cfg(name)
        if v and v not in keys:
            keys.append(v)
    extra = os.getenv("OPENROUTER_KEYS") or os.getenv("OPENROUTER_KEY") or ""
    for part in extra.replace("\n", ",").split(","):
        k = part.strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def _endpoint_for_key(key: str) -> str:
    if key.startswith("sk-or-"):
        return "https://openrouter.ai/api/v1/chat/completions"
    base = _cfg("vision_api_base") or _cfg("VISION_API_BASE") or "https://api.openai.com"
    return f"{base.rstrip('/')}/v1/chat/completions"


def ask_vision(
    prompt: str,
    image_b64: str,
    *,
    models: list[str] | None = None,
    keys: list[str] | None = None,
    max_tokens: int = 900,
    temperature: float = 0.0,
    timeout_seconds: float = 120.0,
    interrupt_checker: InterruptChecker = None,
) -> str | None:
    models = models or [
        _cfg("vision_model", "gpt-4o") or "gpt-4o",
        "gpt-4o-mini",
    ]
    keys = keys or _load_keys()
    if not keys:
        return None
    mtype = "image/jpeg" if image_b64.startswith("/9j/") else "image/png"
    payload_msg = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mtype};base64,{image_b64}"},
                },
            ],
        }
    ]
    for model in models:
        for key in keys:
            if interrupt_checker:
                interrupt_checker()
            try:
                resp = requests.post(
                    _endpoint_for_key(key),
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": payload_msg,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                    timeout=timeout_seconds,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                text = (
                    ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                    or ""
                )
                if looks_like_refusal(text):
                    continue
                return str(text)
            except Exception:
                continue
    return None


def parse_pick_list(text: str) -> list[int]:
    if not text:
        return []
    m = re.search(r"PICK\s*=\s*\[([^\]]*)\]", text, re.I)
    if not m:
        # bare list
        m = re.search(r"\[([^\]]*)\]", text)
    if not m:
        return []
    out: list[int] = []
    for part in m.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(re.sub(r"[^\d-]", "", part)))
        except ValueError:
            continue
    return out


def parse_answer_index(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"ANSWER\s*=\s*(\d+)", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\b\s*$", text.strip())
    if m:
        return int(m.group(1))
    return None


def vote_answer(
    prompt: str,
    image_b64: str,
    *,
    n_options: int | None = None,
    rounds: int = 3,
    answer_format: str = "ANSWER_INDEX",
    timeout_seconds: float = 55.0,
    interrupt_checker: InterruptChecker = None,
) -> dict[str, Any]:
    """Majority vote across up to *rounds* vision calls."""
    raw_texts: list[str] = []
    answers: list[Any] = []
    deadline = time.monotonic() + max(5.0, float(timeout_seconds))
    for i in range(max(1, int(rounds))):
        if time.monotonic() >= deadline:
            break
        if interrupt_checker:
            interrupt_checker()
        text = ask_vision(
            prompt,
            image_b64,
            timeout_seconds=min(60.0, max(5.0, deadline - time.monotonic())),
            interrupt_checker=interrupt_checker,
        )
        if not text:
            continue
        raw_texts.append(text)
        if answer_format.upper() == "PICK_LIST":
            answers.append(tuple(parse_pick_list(text)))
        else:
            idx = parse_answer_index(text)
            if idx is not None and (n_options is None or 0 <= idx < n_options):
                answers.append(idx)
    if not answers:
        return {"answer": None, "votes": {}, "raw_texts": raw_texts, "model_used": "none"}
    counter = Counter(answers)
    best, _ = counter.most_common(1)[0]
    if answer_format.upper() == "PICK_LIST" and isinstance(best, tuple):
        best = list(best)
    return {
        "answer": best,
        "votes": {str(k): v for k, v in counter.items()},
        "raw_texts": raw_texts,
        "model_used": "vision",
    }
