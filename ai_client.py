"""AI client abstraction for coaching suggestions.

Primary provider: Groq (free, low latency). Optional future fallbacks can be added.
Environment:
  GROQ_API_KEY - required for live calls. If absent, a deterministic placeholder is returned.

Public async helper:
  get_coaching_suggestion(context: CoachingContext) -> str

Includes a very small in-memory TTL cache to avoid repeat API calls when the source
content (journal goal + latest entry text) hasn't changed.
"""
from __future__ import annotations
import os
import hashlib
import time
import re
import uuid
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import json

try:
    import httpx  # type: ignore
except ImportError:  # graceful handling if requirements not yet installed
    httpx = None  # type: ignore

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_API_BASE = os.getenv("GROQ_API_BASE", "https://api.groq.com")
CACHE_TTL_SECONDS = int(os.getenv("AI_CACHE_TTL", "600"))  # 10 minutes default

# Simple in-memory cache: key -> (expires_at, value)
_cache: dict[str, tuple[float, str]] = {}

@dataclass
class CoachingContext:
    goal: Optional[str]
    recent_entries: str  # concatenated recent (e.g., last day) entries
    journal_name: Optional[str] = None
    max_tokens: int = 350

    def cache_key(self) -> str:
        h = hashlib.sha256()
        h.update((self.goal or "").encode("utf-8"))
        h.update(self.recent_entries.encode("utf-8"))
        h.update((self.journal_name or "").encode("utf-8"))
        h.update(str(self.max_tokens).encode("ascii"))
        return h.hexdigest()

SYSTEM_PROMPT = (
    "You are a concise motivational coaching assistant. Given a user's stated goal and "
    "their most recent journal entry text (timestamped lines), produce: \n"
    "1. A brief encouragement (1 sentence).\n"
    "2. 2-3 concrete, achievable next actions for the next 24 hours (numbered).\n"
    "3. A single reflective question to prompt deeper thinking.\n"
    "Keep total length under 160 words. Avoid repeating the goal verbatim more than once."
)

async def _call_groq(prompt: str, max_tokens: int, system_prompt: str | None = None) -> str:
    api_key = os.getenv("GROQ_API_KEY")  # dynamic fetch so a restart isn't strictly required after export
    if not api_key:
        return (
            "[Placeholder suggestion]\n"
            "GROQ_API_KEY not detected in environment of running process. Ensure you exported it in the same shell BEFORE starting uvicorn, or load a .env early in main.py."
        )
    if httpx is None:
        return "httpx not installed yet; install requirements to enable AI calls."
    sp = system_prompt or SYSTEM_PROMPT
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": sp},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "stream": False,
    }
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, base_url=GROQ_API_BASE) as client:
            r = await client.post("/openai/v1/chat/completions", headers=headers, json=payload)
            if r.status_code == 401:
                return "Invalid GROQ API key (401)."
            if r.status_code == 429:
                return "Rate limit hit; try again soon or configure a local fallback model."
            if r.status_code >= 500:
                return f"Upstream error {r.status_code}; please retry."
            data = r.json()
            try:
                return data["choices"][0]["message"]["content"].strip()
            except Exception:
                return json.dumps(data)[:800]
    except Exception as e:
        return f"[error] request failed: {type(e).__name__}: {e}"[:400]

async def get_coaching_suggestion(context: CoachingContext) -> str:
    """Return a coaching suggestion, with caching and error handling."""
    key = context.cache_key()
    now = time.time()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    goal_part = context.goal.strip() if context.goal else "(No explicit goal provided)"
    entries_excerpt = context.recent_entries.strip() or "(No recent entries)"

    prompt = (
        f"Goal: {goal_part}\n\n"
        f"Recent journal lines (latest first):\n{entries_excerpt}\n\n"
        "Craft the response following the required 3-section structure."
    )
    suggestion = await _call_groq(prompt, context.max_tokens, system_prompt=SYSTEM_PROMPT)
    _cache[key] = (now + CACHE_TTL_SECONDS, suggestion)
    return suggestion

# ---------------- Goal Breakdown (Plan Generation) -----------------
_PLAN_SYSTEM_PROMPT = (
    "You are an expert accountability coach. Break a user's goal into a concise, numbered, ordered sequence of actionable steps. "
    "Return STRICT JSON ONLY with key 'steps' (array). Each step object fields: id (short slug, kebab-case 3-5 words), title (<=60 chars), description (concise actionable detail <=160 chars), expected_outcome (what success looks like, <=120 chars), order (1-based integer), suggested_duration_days (integer 1-14). Do not include markdown or commentary outside JSON."
)

_plan_json_pattern = re.compile(r"\{[\s\S]*\}")

async def get_goal_breakdown(goal: str, timeframe_days: int | None = None, max_tokens: int = 800) -> List[Dict[str, Any]]:
    """Return a list of plan steps generated by the model.
    timeframe_days: optional hint (e.g., 30) to guide granularity.
    """
    if not goal.strip():
        return []
    tf_hint = f" Target timeframe: ~{timeframe_days} days." if timeframe_days else ""
    user_prompt = f"Goal: {goal.strip()}\n{tf_hint}\nProduce the JSON plan now.".strip()
    raw = await _call_groq(user_prompt, max_tokens=max_tokens, system_prompt=_PLAN_SYSTEM_PROMPT)
    # Attempt to extract JSON
    text = raw.strip()
    match = _plan_json_pattern.search(text)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            raise ValueError("'steps' not list")
    except Exception:
        # Fallback: create a single step summarizing
        return [{
            "id": "define-initial-step",
            "title": "Clarify First Action",
            "description": "Write one concrete action that moves the goal forward today.",
            "expected_outcome": "You have a clearly scoped task to execute next.",
            "order": 1,
            "suggested_duration_days": 1,
        }]
    cleaned: List[Dict[str, Any]] = []
    seen_ids = set()
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        sid = step.get("id") or step.get("title") or f"step-{idx}"
        sid = re.sub(r"[^a-z0-9-]", "-", sid.lower())[:40] or f"step-{idx}"
        if sid in seen_ids:
            sid = f"{sid}-{uuid.uuid4().hex[:4]}"
        seen_ids.add(sid)
        cleaned.append({
            "id": sid,
            "title": (step.get("title") or sid).strip()[:80],
            "description": (step.get("description") or "").strip()[:220],
            "expected_outcome": (step.get("expected_outcome") or "").strip()[:160],
            "order": int(step.get("order") or idx),
            "suggested_duration_days": int(step.get("suggested_duration_days") or 1),
        })
    # Sort by order numeric
    cleaned.sort(key=lambda s: s.get("order", 0))
    return cleaned
