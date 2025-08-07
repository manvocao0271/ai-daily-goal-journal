from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import os

app = FastAPI()

START_DATE = datetime(2025, 8, 4, 0, 6, 0)

@app.get("/api/start-date")
def get_start_date():
    # Return ISO format for JS parsing
    return JSONResponse({"start_date": START_DATE.isoformat()})

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join("static", "index.html"))
