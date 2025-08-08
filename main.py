from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta
from fastapi import HTTPException, status, Depends, Body
import os
from typing import List
from motor.motor_asyncio import AsyncIOMotorClient

app = FastAPI()

START_DATE = datetime(2025, 8, 4, 0, 6, 0)

from passlib.context import CryptContext
from fastapi.templating import Jinja2Templates
from fastapi import Response
from fastapi.responses import RedirectResponse

# Templates for MPA
templates = Jinja2Templates(directory="templates")

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
    if not user_id or not date:
        raise HTTPException(status_code=400, detail="user_id and date required.")
    entry = {"user_id": user_id, "date": date, "text": text}
    if name:
        entry["name"] = name
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
