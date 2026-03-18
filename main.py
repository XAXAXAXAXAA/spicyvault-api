import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "keys.db")
KEY_EXPIRE_MINUTES = int(os.getenv("KEY_EXPIRE_MINUTES", "10"))
GENERATE_COOLDOWN_SECONDS = int(os.getenv("GENERATE_COOLDOWN_SECONDS", "60"))

# =========================
# DATABASE
# =========================
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                used_at TEXT,
                redeemed_in_guild TEXT,
                role_expires_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id TEXT PRIMARY KEY,
                last_generate_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def make_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))

def create_key(user_id: int, guild_id: int) -> tuple[str, datetime]:
    expires_at = utc_now() + timedelta(minutes=KEY_EXPIRE_MINUTES)

    with closing(db_conn()) as conn:
        while True:
            code = make_code()
            exists = conn.execute("SELECT 1 FROM keys WHERE code = ?", (code,)).fetchone()
            if not exists:
                break

        conn.execute(
            """
            INSERT INTO keys (code, user_id, guild_id, created_at, expires_at, used)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (code, str(user_id), str(guild_id), utc_now().isoformat(), expires_at.isoformat()),
        )
        conn.commit()

    return code, expires_at

def get_user_cooldown(user_id: int) -> Optional[datetime]:
    with closing(db_conn()) as conn:
        row = conn.execute(
            "SELECT last_generate_at FROM cooldowns WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row[0])

def set_user_cooldown(user_id: int) -> None:
    with closing(db_conn()) as conn:
        conn.execute(
            """
            INSERT INTO cooldowns (user_id, last_generate_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_generate_at = excluded.last_generate_at
            """,
            (str(user_id), utc_now().isoformat()),
        )
        conn.commit()

# =========================
# FASTAPI
# =========================
app = FastAPI(title="Spicy Vault Key API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://spicyvault.online",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GenerateRequest(BaseModel):
    user_id: int
    guild_id: int

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def root():
    return {"ok": True, "service": "spicyvault-api"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/key/generate")
def api_generate_key(payload: GenerateRequest, request: Request):
    if payload.guild_id != GUILD_ID:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid guild. Expected {GUILD_ID}, got {payload.guild_id}"
        )

    last = get_user_cooldown(payload.user_id)
    if last:
        elapsed = (utc_now() - last).total_seconds()
        if elapsed < GENERATE_COOLDOWN_SECONDS:
            retry_after = GENERATE_COOLDOWN_SECONDS - int(elapsed)
            raise HTTPException(status_code=429, detail=f"Slow down. Try again in {retry_after}s.")

    set_user_cooldown(payload.user_id)
    code, expires_at = create_key(payload.user_id, payload.guild_id)

    return {
        "ok": True,
        "code": code,
        "expires": expires_at.strftime("%Y-%m-%d %H:%M:%S")
    }
