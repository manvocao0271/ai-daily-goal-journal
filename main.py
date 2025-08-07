from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import os
from typing import List

app = FastAPI()

START_DATE = datetime(2025, 8, 4, 0, 6, 0)

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
            for line in f:
                line = line.strip()
                if line:
                    # Expect format: MM/DD/YYYY: entry text
                    if ":" in line:
                        date, text = line.split(":", 1)
                        entries.append({"date": date.strip(), "text": text.strip()})
    return JSONResponse({"entries": entries})

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join("static", "index.html"))

