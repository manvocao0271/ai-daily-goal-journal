from fastapi import FastAPI, Request, HTTPException, status, Depends, Body, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta
import os
from typing import List
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId  # add near top with other imports
from passlib.context import CryptContext
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from ai_client import get_coaching_suggestion, CoachingContext, get_goal_breakdown, get_entry_evaluation  # updated

app = FastAPI()

START_DATE = datetime(2025, 8, 4, 0, 6, 0)

# Templates for MPA
templates = Jinja2Templates(directory="templates")
# Dev convenience: auto reload templates when DEBUG=1
if os.getenv("DEBUG", "0") == "1":
    templates.env.auto_reload = True
    templates.env.cache = {}  # disable template caching

@app.get("/api/start-date")
def get_start_date():
    # Return ISO format for JS parsing
    return JSONResponse({"start_date": START_DATE.isoformat()})

# New endpoint to serve journal entries
@app.get("/api/journal")
def get_journal():
    journal_path = os.path.join(os.path.dirname(__file__), "journal.txt")
    entries: List[dict] = []
    if os.path.exists(journal_path):
        with open(journal_path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if line:
                    today = datetime.now().date()
                    entry_date = (today + timedelta(days=idx-1)).strftime("%m/%d/%Y")
                    entries.append({"date": entry_date, "text": line})
    return JSONResponse({"entries": entries})

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

# Mongo
MONGO_URI = os.environ.get("MONGO_URI")
client = AsyncIOMotorClient(MONGO_URI) if MONGO_URI else AsyncIOMotorClient()
db = client["journal_app"]  # You can name your database
users_collection = db["users"]  # Collection for users
journals_collection = db["journals"]  # Collection for journal entries
# Entries collection (same collection or separate). We'll store entries in a separate collection.
entries_collection = db["journal_entries"]
plan_collection = db["journal_goal_plans"]
evaluations_collection = db["journal_entry_evaluations"]  # new collection for per-day evaluations

# -------- AUTH PAGES (MPA) --------
@app.get("/")
async def root(request: Request):
    # Redirect to login page
    return RedirectResponse(url="/login")

@app.get("/login")
async def login_page(request: Request):
    if request.cookies.get("user_id"):
        return RedirectResponse(url="/journals")
    return templates.TemplateResponse("login.html", {"request": request, "title": "Login"})

@app.get("/journals")
async def journals_page(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("journals.html", {"request": request, "title": "Your Journals", "user_id": user_id})

@app.get("/journals/{journal_id}")
async def journal_detail_page(journal_id: str, request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")
    try:
        oid = ObjectId(journal_id)
    except Exception:
        return RedirectResponse(url="/journals")
    journal = await journals_collection.find_one({"_id": oid, "user_id": user_id})
    if not journal:
        return RedirectResponse(url="/journals")
    created_at = journal.get("created_at")
    if not created_at:
        # derive from ObjectId timestamp
        ts = oid.generation_time  # timezone-aware UTC
        created_at = ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        # Normalize if stored without timezone
        if "Z" not in created_at and "+" not in created_at:
            created_at = created_at.rstrip("Z")
            created_at = created_at.split("+")[0]
            created_at = created_at.replace(" ", "T")
            if not created_at.endswith("Z"):
                created_at += "Z"
    return templates.TemplateResponse("journal.html", {"request": request, "title": journal.get("name") or "Journal", "journal_id": journal_id, "user_id": user_id, "name": journal.get("name"), "goal": journal.get("goal"), "created_at": created_at})

# -------- AUTH API --------
# User registration endpoint
@app.post("/api/register")
async def register_user(user: dict, response: Response):
    username = user.get("username")
    password = user.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required.")
    existing = await users_collection.find_one({"username": username})
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists.")
    hashed_password = get_password_hash(password)
    result = await users_collection.insert_one({"username": username, "hashed_password": hashed_password})
    user_id = str(result.inserted_id)
    # Set session cookie
    response.set_cookie(key="user_id", value=user_id, httponly=True, samesite="lax")
    return {"msg": "User registered successfully.", "user_id": user_id}

# User login endpoint
@app.post("/api/login")
async def login_user(user: dict, response: Response):
    username = user.get("username")
    password = user.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required.")
    db_user = await users_collection.find_one({"username": username})
    if not db_user or not verify_password(password, db_user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    user_id = str(db_user["_id"])
    response.set_cookie(key="user_id", value=user_id, httponly=True, samesite="lax")
    return {"msg": "Login successful.", "user_id": user_id}

@app.post("/api/logout")
async def logout_user(response: Response):
    response.delete_cookie("user_id")
    return {"msg": "Logged out"}

app.mount("/static", StaticFiles(directory="static"), name="static")

# Old index preserved if needed
@app.get("/old")
def serve_old_index():
    return FileResponse(os.path.join("static", "index.html"))

# -------- JOURNAL API --------
# Endpoint to add a journal (with optional name). If user_id is not provided, use cookie.
@app.post("/api/journal")
async def add_journal_entry(request: Request, data: dict = Body(...)):
    user_id = data.get("user_id") or request.cookies.get("user_id")
    text = data.get("text", "")
    date = data.get("date")
    name = data.get("name", None)
    goal = data.get("goal")  # new field
    if not user_id or not date:
        raise HTTPException(status_code=400, detail="user_id and date required.")
    entry = {"user_id": user_id, "date": date, "text": text}
    if name:
        entry["name"] = name
    if goal:
        entry["goal"] = goal
    # Add created_at timestamp if creating a new journal (identified by having a name)
    if name and "created_at" not in entry:
        # Store as explicit UTC (ISO 8601 Z) so client can interpret correctly
        entry["created_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    result = await journals_collection.insert_one(entry)
    return {"msg": "Journal created.", "entry_id": str(result.inserted_id)}

# Endpoint to update journal text or name
@app.put("/api/journal/{journal_id}")
async def update_journal(journal_id: str, data: dict = Body(...)):
    update_fields = {}
    if "text" in data:
        update_fields["text"] = data["text"]
    if "name" in data:
        update_fields["name"] = data["name"]
    if not update_fields:
        raise HTTPException(status_code=400, detail="No fields to update.")
    result = await journals_collection.update_one({"_id": journal_id}, {"$set": update_fields})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Journal not found.")
    return {"msg": "Journal updated."}

# Endpoint to get all journal entries for a user
@app.get("/api/journal/{user_id}")
async def get_user_journal(user_id: str):
    entries = []
    async for entry in journals_collection.find({"user_id": user_id}):
        entry["_id"] = str(entry["_id"])
        entries.append(entry)
    return {"entries": entries}

# Me endpoint using cookie
@app.get("/api/journal/me")
async def get_my_journal(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    entries = []
    async for entry in journals_collection.find({"user_id": user_id}):
        entry["_id"] = str(entry["_id"])
        entries.append(entry)
    return {"entries": entries}

@app.post("/api/journal/{journal_id}/entries")
async def create_entry(journal_id: str, request: Request, data: dict = Body(...)):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        oid = ObjectId(journal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid journal id")
    journal = await journals_collection.find_one({"_id": oid, "user_id": user_id})
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    text = data.get("text", "").strip()
    date = data.get("date")
    client_time = data.get("time")  # new optional field
    if not text:
        raise HTTPException(status_code=400, detail="Text required")
    if not date:
        # Keep using client-provided date normally, fallback to server local date
        date = datetime.now().strftime("%m/%d/%Y")
    # Use client local time if provided; else server local time (not UTC)
    time_str = client_time or datetime.now().strftime("%H:%M:%S")
    line = f"[{time_str}] {text}"
    existing = await entries_collection.find_one({"journal_id": journal_id, "user_id": user_id, "date": date})
    if existing:
        new_text = (existing.get("text") + "\n" + line) if existing.get("text") else line
        await entries_collection.update_one({"_id": existing["_id"]}, {"$set": {"text": new_text}})
        return {"msg": "Entry appended", "entry_id": str(existing["_id"]) }
    doc = {"journal_id": journal_id, "user_id": user_id, "text": line, "date": date}
    res = await entries_collection.insert_one(doc)
    return {"msg": "Entry created", "entry_id": str(res.inserted_id)}

@app.get("/api/journal/{user_id}/{journal_id}/entries")
async def list_entries(user_id: str, journal_id: str, request: Request):
    cookie_uid = request.cookies.get("user_id")
    if cookie_uid != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        oid = ObjectId(journal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid journal id")
    journal = await journals_collection.find_one({"_id": oid, "user_id": user_id})
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    entries = []
    async for e in entries_collection.find({"journal_id": journal_id, "user_id": user_id}):
        e["_id"] = str(e["_id"])
        entries.append(e)
    return {"entries": entries}

@app.put("/api/journal/entry/{entry_id}")
async def update_entry(entry_id: str, data: dict = Body(...), request: Request = None):
    user_id = request.cookies.get("user_id") if request else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    text = data.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text required")
    try:
        eid = ObjectId(entry_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid entry id")
    result = await entries_collection.update_one({"_id": eid, "user_id": user_id}, {"$set": {"text": text}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"msg": "Entry updated"}

@app.delete("/api/journal/entry/{entry_id}")
async def delete_entry(entry_id: str, request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        eid = ObjectId(entry_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid entry id")
    result = await entries_collection.delete_one({"_id": eid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"msg": "Entry deleted"}

@app.post("/api/coach/suggest")
async def coach_suggest(request: Request, payload: dict = Body(...)):
    """Return a short structured coaching suggestion based on goal + recent entry text.
    Body fields:
      journal_id: str (required) - used to fetch goal + recent entry lines
      limit_days: int (optional, default 1) - how many days of entries to include (aggregate newest first)
      max_tokens: int (optional)
    """
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    journal_id = payload.get("journal_id")
    if not journal_id:
        raise HTTPException(status_code=400, detail="journal_id required")
    try:
        oid = ObjectId(journal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid journal id")
    journal = await journals_collection.find_one({"_id": oid, "user_id": user_id})
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    # Collect recent entries
    limit_days = int(payload.get("limit_days", 1))
    if limit_days < 1:
        limit_days = 1
    cutoff_dates = set()
    # Build set of recent date strings (MM/DD/YYYY) up to limit_days back
    now = datetime.now()
    for i in range(limit_days):
        cutoff_dates.add((now - timedelta(days=i)).strftime("%m/%d/%Y"))
    lines = []
    async for e in entries_collection.find({"journal_id": journal_id, "user_id": user_id, "date": {"$in": list(cutoff_dates)}}):
        # Newest first by ObjectId desc: buffer first then sort
        lines.append((e["_id"], e.get("text", "")))
    # Sort newest first by ObjectId (descending)
    lines.sort(key=lambda t: str(t[0]), reverse=True)
    aggregated = []
    for _, txt in lines:
        if txt:
            # Keep line order inside a day as-is; prepend day groups newest first
            aggregated.extend(reversed(txt.splitlines()))  # reversed so latest appended lines come first
    recent_excerpt = "\n".join(aggregated[:120])  # safety cap
    context = CoachingContext(goal=journal.get("goal"), recent_entries=recent_excerpt, journal_name=journal.get("name"), max_tokens=int(payload.get("max_tokens", 350)))
    suggestion = await get_coaching_suggestion(context)
    return {"suggestion": suggestion}

@app.post("/api/coach/breakdown")
async def coach_breakdown(request: Request, payload: dict = Body(...)):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    journal_id = payload.get("journal_id")
    if not journal_id:
        raise HTTPException(status_code=400, detail="journal_id required")
    try:
        oid = ObjectId(journal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid journal id")
    journal = await journals_collection.find_one({"_id": oid, "user_id": user_id})
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    goal = journal.get("goal") or ""
    steps = await get_goal_breakdown(goal)
    steps = _normalize_steps(steps)
    await plan_collection.update_one(
        {"journal_id": journal_id, "user_id": user_id},
        {"$set": {"steps": steps, "updated_at": datetime.utcnow().isoformat()+"Z", "goal_snapshot": goal}},
        upsert=True,
    )
    return {"steps": steps}

# ---- Helper: normalize plan steps count/order ----
def _normalize_steps(steps):
    if not isinstance(steps, list):
        steps = []
    steps = [s for s in steps if isinstance(s, dict)]
    try:
        steps.sort(key=lambda s: s.get('order', 0))
    except Exception:
        pass
    # Trim to 8 but do NOT force padding
    if len(steps) > 8:
        steps = steps[:8]
    for i, s in enumerate(steps, start=1):
        s['order'] = i
        if 'completed' not in s:
            s['completed'] = False
    return steps

@app.get("/api/coach/plan/{journal_id}")
async def coach_get_plan(journal_id: str, request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    doc = await plan_collection.find_one({"journal_id": journal_id, "user_id": user_id})
    if not doc:
        return {"steps": []}
    raw_steps = doc.get("steps", [])
    steps = _normalize_steps(raw_steps)
    if raw_steps != steps:
        await plan_collection.update_one({"_id": doc['_id']}, {"$set": {"steps": steps}})
    doc["_id"] = str(doc["_id"])
    return {"steps": steps}

@app.post("/api/coach/plan/{journal_id}/toggle")
async def toggle_step_completion(journal_id: str, payload: dict = Body(...), request: Request = None):
    user_id = request.cookies.get("user_id") if request else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    step_id = payload.get("step_id")
    completed = payload.get("completed")
    if step_id is None or completed is None:
        raise HTTPException(status_code=400, detail="step_id and completed required")
    # Use positional operator
    result = await plan_collection.update_one(
        {"journal_id": journal_id, "user_id": user_id, "steps.id": step_id},
        {"$set": {"steps.$.completed": bool(completed), "steps.$.completed_at": datetime.utcnow().isoformat()+"Z" if completed else None}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Step not found")
    return {"msg": "Updated"}

@app.post("/api/coach/evaluate/{journal_id}")
async def coach_evaluate_entry(journal_id: str, request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        oid = ObjectId(journal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid journal id")
    journal = await journals_collection.find_one({"_id": oid, "user_id": user_id})
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    # Resolve today's date (UTC) and both padded & non-padded forms used by entries
    today_dt = datetime.utcnow().date()
    iso_today = today_dt.isoformat()  # YYYY-MM-DD
    padded = today_dt.strftime("%m/%d/%Y")
    unpadded = f"{today_dt.month}/{today_dt.day}/{today_dt.year}"
    # If an evaluation already exists for today, return it directly
    existing_eval = await evaluations_collection.find_one({"journal_id": journal_id, "user_id": user_id, "date": iso_today})
    if existing_eval:
        return {"evaluation": existing_eval.get("evaluation", "")}
    # Fetch today's entry (single document aggregated by create_entry logic)
    entry_doc = await entries_collection.find_one({"journal_id": journal_id, "user_id": user_id, "date": {"$in": [padded, unpadded]}})
    if not entry_doc:
        raise HTTPException(status_code=400, detail="No entry found for today to evaluate")
    entry_text = entry_doc.get("text", "")
    evaluation = await get_entry_evaluation(journal.get("goal"), entry_text, journal.get("name"), iso_today)
    # Store evaluation (ephemeral: keep only today; cleanup older for this journal)
    await evaluations_collection.delete_many({"journal_id": journal_id, "user_id": user_id, "date": {"$ne": iso_today}})
    await evaluations_collection.insert_one({
        "journal_id": journal_id,
        "user_id": user_id,
        "date": iso_today,
        "evaluation": evaluation,
        "created_at": datetime.utcnow().isoformat()+"Z"
    })
    return {"evaluation": evaluation}
