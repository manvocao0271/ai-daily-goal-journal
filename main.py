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

# User registration endpoint
@app.post("/api/register")
async def register_user(user: dict):
    username = user.get("username")
    password = user.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required.")
    existing = await users_collection.find_one({"username": username})
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists.")
    hashed_password = get_password_hash(password)
    result = await users_collection.insert_one({"username": username, "hashed_password": hashed_password})
    return {"msg": "User registered successfully.", "user_id": str(result.inserted_id)}

# User login endpoint
@app.post("/api/login")
async def login_user(user: dict):
    username = user.get("username")
    password = user.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required.")
    db_user = await users_collection.find_one({"username": username})
    if not db_user or not verify_password(password, db_user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return {"msg": "Login successful.", "user_id": str(db_user["_id"])}

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join("static", "index.html"))

MONGO_URI = os.environ.get("MONGO_URI")
client = AsyncIOMotorClient(MONGO_URI)
db = client["journal_app"]  # You can name your database
users_collection = db["users"]  # Collection for users
journals_collection = db["journals"]  # Collection for journal entries

# Endpoint to add a journal entry for a user
@app.post("/api/journal")
async def add_journal_entry(data: dict = Body(...)):
    user_id = data.get("user_id")
    text = data.get("text")
    date = data.get("date")
    if not user_id or not text or not date:
        raise HTTPException(status_code=400, detail="user_id, text, and date required.")
    entry = {"user_id": user_id, "date": date, "text": text}
    result = await journals_collection.insert_one(entry)
    return {"msg": "Entry added.", "entry_id": str(result.inserted_id)}

# Endpoint to get all journal entries for a user
@app.get("/api/journal/{user_id}")
async def get_user_journal(user_id: str):
    entries = []
    async for entry in journals_collection.find({"user_id": user_id}):
        entry["_id"] = str(entry["_id"])
        entries.append(entry)
    return {"entries": entries}
