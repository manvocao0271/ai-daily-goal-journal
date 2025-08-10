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
    "You are an expert accountability coach. Break a user's goal into EXACTLY 8 clear, ordered, actionable steps that advance the goal directly. "
    "The goal is already defined; DO NOT include any step about defining, clarifying, refining, restating, or setting the goal itself. Start with the first concrete action. "
    "Return STRICT JSON ONLY with key 'steps' (array of length 8). Each step object fields: id (kebab-case 3-6 words), title (<=60 chars), description (<=160 chars), expected_outcome (<=120 chars), order (1-based integer). "
    "Do NOT include status, duration, extra keys, commentary, markdown, or text outside JSON."
)

_plan_json_pattern = re.compile(r"\{[\s\S]*\}")

async def get_goal_breakdown(goal: str, max_tokens: int = 900) -> List[Dict[str, Any]]:
    """Return up to 8 ordered plan steps for the goal (trim to 8), excluding meta goal-definition steps."""
    if not goal.strip():
        return []
    user_prompt = f"Goal: {goal.strip()}\nProduce the JSON now.".strip()
    raw = await _call_groq(user_prompt, max_tokens=max_tokens, system_prompt=_PLAN_SYSTEM_PROMPT)
    text = raw.strip()
    match = _plan_json_pattern.search(text)
    if match:
        text = match.group(0)
    steps: List[Dict[str, Any]] = []
    try:
        data = json.loads(text)
        raw_steps = data.get("steps") if isinstance(data, dict) else None
        if isinstance(raw_steps, list):
            for idx, step in enumerate(raw_steps, start=1):
                if not isinstance(step, dict):
                    continue
                title = (step.get("title") or "").strip()
                desc = (step.get("description") or "").strip()
                lower_combo = f"{title} {desc}".lower()
                if "define" in lower_combo and "goal" in lower_combo:
                    continue  # skip meta goal-def step
                if "clarify" in lower_combo and "goal" in lower_combo:
                    continue
                sid = step.get("id") or title or f"step-{idx}"
                sid = re.sub(r"[^a-z0-9-]", "-", sid.lower()).strip("-")[:48] or f"step-{idx}"
                steps.append({
                    "id": sid,
                    "title": title[:80] or sid,
                    "description": desc[:220],
                    "expected_outcome": (step.get("expected_outcome") or "").strip()[:160],
                    "order": int(step.get("order") or idx),
                })
    except Exception:
        steps = []
    steps.sort(key=lambda s: s.get("order", 0))
    # Trim to 8 and renumber
    steps = steps[:8]
    for i, s in enumerate(steps, start=1):
        s["order"] = i
    # If fewer than 8 remain, we just return that count (no padding to avoid generic filler)
    return steps
