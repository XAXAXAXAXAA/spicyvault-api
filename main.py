import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # 🔐 TOKEN IZ ENV
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ROLE_ID = int(os.getenv("ROLE_ID", "0"))

DATABASE_PATH = "keys.db"
KEY_EXPIRE_MINUTES = 10

# =========================
# DATABASE
# =========================

def utc_now():
    return datetime.now(timezone.utc)

def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS keys (
        code TEXT PRIMARY KEY,
        user_id TEXT,
        expires_at TEXT,
        used INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()

def create_key(user_id):
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    expires = utc_now() + timedelta(minutes=KEY_EXPIRE_MINUTES)

    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute(
        "INSERT INTO keys (code, user_id, expires_at, used) VALUES (?, ?, ?, 0)",
        (code, str(user_id), expires.isoformat())
    )
    conn.commit()
    conn.close()

    return code, expires

# =========================
# FASTAPI
# =========================

app = FastAPI()

class GenerateRequest(BaseModel):
    user_id: int

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/key/generate")
def generate_key(data: GenerateRequest):
    code, expires = create_key(data.user_id)

    return {
        "code": code,
        "expires": expires.strftime("%Y-%m-%d %H:%M:%S")
    }

# =========================
# START
# =========================

init_db()
