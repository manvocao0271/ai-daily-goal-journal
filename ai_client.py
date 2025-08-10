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
from dataclasses import dataclass
from typing import Optional
import json

try:
    import httpx  # type: ignore
except ImportError:  # graceful handling if requirements not yet installed
    httpx = None  # type: ignore

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_API_BASE = os.getenv("GROQ_API_BASE", "https://api.groq.com")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
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

async def _call_groq(prompt: str, max_tokens: int) -> str:
    if not GROQ_API_KEY:
        return (
            "[Placeholder suggestion]\n"
            "Set GROQ_API_KEY to get live AI coaching. Meanwhile: Focus on one small, high-impact action" \
            " you can finish today; write it down with a time block."
        )
    if httpx is None:
        return "httpx not installed yet; install requirements to enable AI calls."

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "stream": False,
    }
    timeout = httpx.Timeout(15.0, connect=10.0)
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
    suggestion = await _call_groq(prompt, context.max_tokens)
    _cache[key] = (now + CACHE_TTL_SECONDS, suggestion)
    return suggestion
