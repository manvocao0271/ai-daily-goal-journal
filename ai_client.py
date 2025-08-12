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
    "The goal is already defined; DO NOT include any step about defining, clarifying, refining, restating, or setting goals (e.g., 'Set Personal Goals', 'Set SMART Goals', 'Establish Goals') or generic meta-planning (e.g., 'Create an Action Plan', 'Plan Your Approach'). Start with the first concrete action. "
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
                if _is_meta_goal_step(lower_combo):
                    continue  # skip meta goal-setting/definition steps
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
    # Sort by order
    steps.sort(key=lambda s: s.get("order", 0))
    # If we filtered out meta steps and have fewer than 8, try to backfill with concrete steps
    if goal.strip() and len(steps) < 8:
        try:
            needed = 8 - len(steps)
            backfill = await _generate_concrete_steps(goal, existing_titles=[s.get("title","") for s in steps], start_order=len(steps)+1, count=needed)
            # Filter any lingering meta/dupe and extend
            for st in backfill:
                title = (st.get("title") or "").strip()
                desc = (st.get("description") or "").strip()
                if not title:
                    continue
                if _is_meta_goal_step(f"{title} {desc}".lower()):
                    continue
                # ensure unique by title id
                if any(title.lower()==x.get("title"," ").lower() for x in steps):
                    continue
                steps.append({
                    "id": re.sub(r"[^a-z0-9-]","-", (st.get("id") or title).lower()).strip("-")[:48] or f"step-{len(steps)+1}",
                    "title": title[:80],
                    "description": desc[:220],
                    "expected_outcome": (st.get("expected_outcome") or "").strip()[:160],
                    "order": int(st.get("order") or len(steps)+1),
                })
        except Exception:
            pass
    # Trim to 8 and renumber
    steps = steps[:8]
    for i, s in enumerate(steps, start=1):
        s["order"] = i
    return steps

def _is_meta_goal_step(lower_combo: str) -> bool:
    """Return True if a step text appears to be about setting/defining goals rather than taking action."""
    if not lower_combo:
        return False
    # Common meta patterns
    patterns = [
        r"\bset(ting)?\b[^\n]*\bgoals?\b",
        r"\bdefine\b[^\n]*\bgoals?\b",
        r"\bclarify\b[^\n]*\bgoals?\b",
        r"\brefine\b[^\n]*\bgoals?\b",
        r"\bestablish\b[^\n]*\bgoals?\b",
        r"\bdetermine\b[^\n]*\bgoals?\b",
        r"\barticulate\b[^\n]*\bgoals?\b",
        r"\bchoose\b[^\n]*\bgoals?\b",
        r"\bidentify\b[^\n]*\bgoals?\b",
        r"\bgoal[-\s]?setting\b",
        r"\bsmart\b[^\n]*\bgoals?\b",
        r"\bset personal goals\b",
    # Meta planning phrases (avoid generic 'action plan')
    r"\b(create|develop|build|draft|make|outline|design)\b[^\n]*\baction\s*plan\b",
    r"\bplan\s+your\s+(approach|actions|steps)\b",
    ]
    for pat in patterns:
        if re.search(pat, lower_combo):
            return True
    # Avoid excluding legitimate steps that mention "goal" incidentally; only above patterns trigger
    return False

async def _generate_concrete_steps(goal: str, existing_titles: List[str], start_order: int, count: int) -> List[Dict[str, Any]]:
    """Ask the model for additional concrete steps that advance the goal directly, avoiding meta goal-setting. Returns raw step dicts."""
    existing_titles = [t.strip() for t in existing_titles if isinstance(t, str)]
    titles_blob = "\n".join(f"- {t}" for t in existing_titles if t)
    system = (
        "You add missing steps for a user's already-defined goal. "
        "Add ONLY concrete actions that directly advance the goal. "
        "Do NOT include any step about defining/clarifying/refining/setting goals (no SMART goals) or generic planning like 'Create an Action Plan'. "
        "Return STRICT JSON with key 'steps' (array length EXACTLY N) with fields id, title, description, expected_outcome, order. "
        "Titles must be distinct from the provided existing titles."
    )
    user = (
        f"Goal: {goal}\n"
        f"Existing step titles (avoid duplicates):\n{titles_blob or '- (none)'}\n\n"
        f"Generate EXACTLY {count} additional steps continuing from order {start_order}."
    )
    raw = await _call_groq(user, max_tokens=700, system_prompt=system)
    text = raw.strip()
    m = _plan_json_pattern.search(text)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            return data["steps"]
    except Exception:
        return []
    return []

# -------- Entry Evaluation (Daily Impact) --------
_EVAL_SYSTEM_PROMPT = (
    "You are a supportive progress evaluator. Given a user's overarching goal and a single day's journal entry (timestamped lines), produce a concise evaluation. "
    "Format sections exactly in this order (no headings beyond those labels):\n"
    "1. Micro Summary: <one sentence>\n"
    "2. Impact (0-5): <score> - <short justification>\n"
    "3. Progress Signals: <2-3 comma-separated concise signals OR 'None noted'>\n"
    "4. Next Micro Focus: <one actionable focus for next day>\n"
    "5. Encouragement: <motivational sentence ending positively>.\n"
    "Keep total under 170 words; be specific; never restate the full goal verbatim."
)

async def get_entry_evaluation(goal: str | None, entry_text: str, journal_name: str | None, date_str: str) -> str:
    goal_part = goal.strip() if goal else "(No explicit goal)"
    excerpt = entry_text.strip() or "(No content recorded)"
    user_prompt = (
        f"Goal: {goal_part}\nDate: {date_str}\nJournal: {journal_name or 'Journal'}\nEntry Lines:\n{excerpt}\n\nGenerate the evaluation now."  # date included to encourage variability
    )
    # Cache key includes date to force new day regeneration while still deduping repeated calls same day
    h = hashlib.sha256(); h.update(goal_part.encode()); h.update(excerpt.encode()); h.update(date_str.encode()); key = 'eval:' + h.hexdigest()
    cached = _cache.get(key)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]
    result = await _call_groq(user_prompt, max_tokens=300, system_prompt=_EVAL_SYSTEM_PROMPT)
    _cache[key] = (now + CACHE_TTL_SECONDS, result)
    return result
