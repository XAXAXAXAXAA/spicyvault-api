import asyncio
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Optional
from urllib.parse import quote_plus

import discord
from discord import app_commands
from discord.ext import commands, tasks
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
import uvicorn

# =========================
# CONFIG
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "MTQ4MjI5NzEzNTM5NDMyODU5Ng.GfyVDL.u1mOHC8eJrEWmu94ozZLlsXknqv6Pr4XtXkqKQ")
GUILD_ID = int(os.getenv("GUILD_ID", "1482121264033169504"))
ROLE_ID = int(os.getenv("ROLE_ID", "1483043990512341163"))
BOT_CHANNEL_ID = int(os.getenv("BOT_CHANNEL_ID", "1483228110358057131"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
DATABASE_PATH = os.getenv("DATABASE_PATH", "keys.db")
ROLE_DURATION_MINUTES = int(os.getenv("ROLE_DURATION_MINUTES", "60"))
KEY_EXPIRE_MINUTES = int(os.getenv("KEY_EXPIRE_MINUTES", "10"))
GENERATE_COOLDOWN_SECONDS = int(os.getenv("GENERATE_COOLDOWN_SECONDS", "60"))
WEB_SHARED_TOKEN = os.getenv("WEB_SHARED_TOKEN", "change-me")
PORT = int(os.getenv("PORT", "8000"))
BRAND_NAME = os.getenv("BRAND_NAME", "Spicy Vault")
ACCENT_HEX = os.getenv("ACCENT_HEX", "8B5CF6")

if DISCORD_TOKEN == "MTQ4MjI5NzEzNTM5NDMyODU5Ng.GfyVDL.u1mOHC8eJrEWmu94ozZLlsXknqv6Pr4XtXkqKQ":
    print("WARNING: Set DISCORD_TOKEN before running.")

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


def get_key(code: str) -> Optional[sqlite3.Row]:
    with closing(db_conn()) as conn:
        row = conn.execute("SELECT * FROM keys WHERE code = ?", (code.upper(),)).fetchone()
        return row


def mark_key_used(code: str, guild_id: int, role_expires_at: datetime) -> None:
    with closing(db_conn()) as conn:
        conn.execute(
            """
            UPDATE keys
            SET used = 1,
                used_at = ?,
                redeemed_in_guild = ?,
                role_expires_at = ?
            WHERE code = ?
            """,
            (utc_now().isoformat(), str(guild_id), role_expires_at.isoformat(), code.upper()),
        )
        conn.commit()


def get_user_cooldown(user_id: int) -> Optional[datetime]:
    with closing(db_conn()) as conn:
        row = conn.execute("SELECT last_generate_at FROM cooldowns WHERE user_id = ?", (str(user_id),)).fetchone()
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


def get_expired_role_records() -> list[sqlite3.Row]:
    with closing(db_conn()) as conn:
        return conn.execute(
            """
            SELECT * FROM keys
            WHERE used = 1
              AND role_expires_at IS NOT NULL
              AND role_expires_at <= ?
            """,
            (utc_now().isoformat(),),
        ).fetchall()


def clear_role_expiry(code: str) -> None:
    with closing(db_conn()) as conn:
        conn.execute("UPDATE keys SET role_expires_at = NULL WHERE code = ?", (code.upper(),))
        conn.commit()


# =========================
# WEB APP
# =========================
# =========================
# WEB APP
# =========================
app = FastAPI(title="Key System")

@app.get("/health")
def health():
    return {"ok": True}

PORT = int(os.getenv("PORT", "8000"))
WEB_SHARED_TOKEN = os.getenv("WEB_SHARED_TOKEN", "change-me")
BRAND_NAME = os.getenv("BRAND_NAME", "Spicy Vault")


def html_shell(title: str, body: str) -> str:
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg:#09070f;
      --bg2:#120d1e;
      --card:rgba(17,14,29,.86);
      --line:rgba(167,139,250,.18);
      --line-2:rgba(196,181,253,.26);
      --text:#f5f3ff;
      --muted:#b7abc9;
      --accent:#8b5cf6;
      --accent-2:#a855f7;
      --accent-3:#c084fc;
      --success:#22c55e;
      --shadow:0 30px 80px rgba(0,0,0,.42);
      --radius:28px;
    }}
    * {{ box-sizing:border-box; }}
    html, body {{ margin:0; min-height:100%; }}
    body {{
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color:var(--text);
      background:
        radial-gradient(circle at 15% 20%, rgba(139,92,246,.20), transparent 28%),
        radial-gradient(circle at 85% 15%, rgba(192,132,252,.16), transparent 24%),
        radial-gradient(circle at 50% 80%, rgba(168,85,247,.12), transparent 30%),
        linear-gradient(180deg, var(--bg), var(--bg2));
      overflow-x:hidden;
    }}
    .bg-grid {{
      position:fixed;
      inset:0;
      background-image:
        linear-gradient(rgba(255,255,255,.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.03) 1px, transparent 1px);
      background-size:44px 44px;
      mask-image: radial-gradient(circle at center, black, transparent 85%);
      pointer-events:none;
      opacity:.45;
    }}
    .wrap {{
      min-height:100vh;
      display:flex;
      align-items:center;
      justify-content:center;
      padding:36px 18px;
      position:relative;
      z-index:2;
    }}
    .card {{
      width:min(100%, 560px);
      background:var(--card);
      border:1px solid var(--line);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      backdrop-filter: blur(18px);
      overflow:hidden;
    }}
    .hero {{
      padding:28px 28px 18px;
      border-bottom:1px solid rgba(255,255,255,.05);
      background:linear-gradient(180deg, rgba(139,92,246,.10), rgba(255,255,255,0));
    }}
    .brand {{
      display:inline-flex;
      align-items:center;
      gap:10px;
      padding:10px 14px;
      border-radius:999px;
      background:rgba(255,255,255,.04);
      border:1px solid rgba(255,255,255,.07);
      color:#efe9ff;
      font-size:13px;
      letter-spacing:.08em;
      text-transform:uppercase;
    }}
    .shield {{
      width:28px;
      height:28px;
      border-radius:10px;
      background:linear-gradient(135deg, var(--accent), var(--accent-3));
      display:grid;
      place-items:center;
      font-weight:800;
    }}
    h1 {{ margin:18px 0 10px; font-size:38px; line-height:1.04; letter-spacing:-.03em; }}
    .sub {{ color:var(--muted); font-size:15px; line-height:1.65; margin:0; }}
    .body {{ padding:24px 28px 28px; }}
    .steps {{
      display:grid;
      gap:12px;
      margin:0 0 22px;
      padding:0;
      list-style:none;
    }}
    .step {{
      display:flex;
      gap:14px;
      align-items:flex-start;
      padding:14px 15px;
      border-radius:18px;
      border:1px solid rgba(255,255,255,.06);
      background:rgba(255,255,255,.025);
    }}
    .num {{
      width:30px;
      min-width:30px;
      height:30px;
      border-radius:999px;
      display:grid;
      place-items:center;
      background:rgba(139,92,246,.18);
      border:1px solid rgba(139,92,246,.35);
      color:#e9ddff;
      font-weight:700;
      font-size:13px;
    }}
    .step strong {{ display:block; margin-bottom:3px; font-size:15px; }}
    .step span {{ color:var(--muted); font-size:14px; line-height:1.5; }}
    .key-box {{
      margin:12px 0 18px;
      padding:22px 18px;
      border-radius:24px;
      background:
        linear-gradient(180deg, rgba(139,92,246,.12), rgba(255,255,255,.02)),
        rgba(10,8,18,.85);
      border:1px solid rgba(167,139,250,.22);
    }}
    .label {{ color:#cfc3e9; font-size:12px; text-transform:uppercase; letter-spacing:.16em; margin-bottom:10px; }}
    .code-row {{ display:flex; gap:10px; flex-wrap:wrap; justify-content:center; }}
    .code-cell {{
      width:66px;
      height:76px;
      border-radius:18px;
      display:grid;
      place-items:center;
      font-size:34px;
      font-weight:800;
      color:#f7f2ff;
      background:linear-gradient(180deg, rgba(139,92,246,.18), rgba(139,92,246,.08));
      border:1px solid rgba(196,181,253,.28);
    }}
    .meta {{ color:var(--muted); text-align:center; font-size:14px; margin-top:14px; line-height:1.6; }}
    .actions {{ display:grid; gap:12px; margin-top:18px; }}
    .btn {{
      appearance:none;
      border:none;
      text-decoration:none;
      text-align:center;
      padding:15px 18px;
      border-radius:18px;
      font-weight:700;
      font-size:15px;
    }}
    .btn-primary {{
      color:white;
      background:linear-gradient(135deg, var(--accent), #a855f7);
    }}
    .btn-secondary {{
      color:#efe9ff;
      background:rgba(255,255,255,.04);
      border:1px solid rgba(255,255,255,.08);
    }}
    .footer-grid {{
      display:grid;
      grid-template-columns:repeat(3,1fr);
      gap:12px;
      margin-top:18px;
    }}
    .mini {{
      padding:14px 12px;
      border-radius:18px;
      background:rgba(255,255,255,.03);
      border:1px solid rgba(255,255,255,.06);
      text-align:center;
    }}
    .mini b {{ display:block; font-size:12px; letter-spacing:.1em; text-transform:uppercase; color:#d8cff0; margin-bottom:6px; }}
    .mini span {{ color:var(--muted); font-size:13px; }}
  </style>
</head>
<body>
  <div class="bg-grid"></div>
  <div class="wrap">{body}</div>
</body>
</html>
"""


def render_front_page(user_id: int, guild_id: int) -> str:
    generate_link = f"/generate?user_id={user_id}&guild_id={guild_id}&token={WEB_SHARED_TOKEN}"
    body = f"""
    <section class="card">
      <div class="hero">
        <div class="brand"><span class="shield">K</span> {BRAND_NAME} Access</div>
        <h1>Unlock VIP access with a one-time key</h1>
        <p class="sub">Generate a private code linked to your Discord account. Redeem it in your server and get temporary access instantly.</p>
      </div>
      <div class="body">
        <ul class="steps">
          <li class="step"><div class="num">1</div><div><strong>Generate your key</strong><span>Press the button below to create a one-time code tied to your Discord account.</span></div></li>
          <li class="step"><div class="num">2</div><div><strong>Copy and redeem it</strong><span>Return to Discord, click <b>Redeem Key</b>, and paste the code exactly as shown.</span></div></li>
          <li class="step"><div class="num">3</div><div><strong>Get temporary access</strong><span>If the code is valid, your VIP role is granted for {ROLE_DURATION_MINUTES} minutes.</span></div></li>
        </ul>
        <div class="actions">
          <a class="btn btn-primary" href="{generate_link}">Generate Key</a>
          <a class="btn btn-secondary" href="discord://-/channels/{guild_id}/{BOT_CHANNEL_ID}">Back to Discord</a>
        </div>
        <div class="footer-grid">
          <div class="mini"><b>Verified</b><span>Bound to your Discord ID</span></div>
          <div class="mini"><b>One-Time Use</b><span>Each key works once only</span></div>
          <div class="mini"><b>Fast Expiry</b><span>Redeem within {KEY_EXPIRE_MINUTES} minutes</span></div>
        </div>
      </div>
    </section>
    """
    return html_shell(f"{BRAND_NAME} | Access", body)


def render_key_page(code: str, expires_label: str, guild_id: int) -> str:
    code_cells = "".join(f'<div class="code-cell">{char}</div>' for char in code)
    body = f"""
    <section class="card">
      <div class="hero">
        <div class="brand"><span class="shield">K</span> {BRAND_NAME} Access</div>
        <h1>Here is your access code</h1>
        <p class="sub">This key is one-time use, linked to your Discord account, and must be redeemed before it expires.</p>
      </div>
      <div class="body">
        <div class="key-box">
          <div class="label">Access Key</div>
          <div class="code-row">{code_cells}</div>
          <div class="meta">Expires at <b>{expires_label}</b></div>
        </div>
        <div class="actions">
          <a class="btn btn-primary" href="discord://-/channels/{guild_id}/{BOT_CHANNEL_ID}">Return to Discord</a>
          <a class="btn btn-secondary" href="/?user_id=123&guild_id={guild_id}">Home</a>
        </div>
        <div class="footer-grid">
          <div class="mini"><b>Verified</b><span>User-bound validation</span></div>
          <div class="mini"><b>Encrypted</b><span>Unique one-time code</span></div>
          <div class="mini"><b>Audit Logged</b><span>Redeem activity tracked</span></div>
        </div>
      </div>
    </section>
    """
    return html_shell(f"{BRAND_NAME} | Your Key", body)


@app.get("/", response_class=HTMLResponse)
def front_page(user_id: int = Query(123), guild_id: int = Query(GUILD_ID)):
    return HTMLResponse(render_front_page(user_id, guild_id))


@app.get("/generate", response_class=HTMLResponse)
def generate_page(user_id: int = Query(...), guild_id: int = Query(...), token: str = Query(...)):
    if token != WEB_SHARED_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    last = get_user_cooldown(user_id)
    if last and (utc_now() - last).total_seconds() < GENERATE_COOLDOWN_SECONDS:
        retry_after = GENERATE_COOLDOWN_SECONDS - int((utc_now() - last).total_seconds())
        return HTMLResponse(f"<h1>Slow down. Try again in {retry_after}s.</h1>", status_code=429)

    set_user_cooldown(user_id)
    code, expires_at = create_key(user_id, guild_id)
    return HTMLResponse(
        render_key_page(
            code=code,
            expires_label=expires_at.strftime("%Y-%m-%d %H:%M UTC"),
            guild_id=guild_id,
        )
    )
# =========================
# DISCORD BOT
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
TREE_GUILD = discord.Object(id=GUILD_ID)


class KeyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Key", style=discord.ButtonStyle.primary, custom_id="key_generate")
    async def generate_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        url = f"{PUBLIC_BASE_URL}/?user_id={interaction.user.id}&guild_id={interaction.guild_id}"
        embed = discord.Embed(
            title=f"{BRAND_NAME} • VIP Access",
            description=(
                "**How it works**\n"
                "• Click **Open Key Page**\n"
                "• Generate your one-time code\n"
                "• Return and press **Redeem Key**\n"
                f"• If valid, you get access for **{ROLE_DURATION_MINUTES} minutes**"
            ),
            color=0x8B5CF6,
        )
        embed.add_field(name="Secure", value="Code is tied to your Discord account.", inline=False)
        embed.add_field(name="Key page", value=f"[Open Key Page]({url})", inline=False)
        embed.set_footer(text="One-time use • Fast expiry • Account-linked")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Redeem Key", style=discord.ButtonStyle.success, custom_id="key_redeem")
    async def redeem_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RedeemModal())


class RedeemModal(discord.ui.Modal, title="Redeem your key"):
    key_input = discord.ui.TextInput(label="Enter your key", placeholder="Example: ARM36V", min_length=4, max_length=32)

    async def on_submit(self, interaction: discord.Interaction):
        code = str(self.key_input).strip().upper()
        row = get_key(code)

        if not row:
            await interaction.response.send_message("Invalid key.", ephemeral=True)
            return
        if row["user_id"] != str(interaction.user.id):
            await interaction.response.send_message("This key was not generated for your account.", ephemeral=True)
            return
        if row["guild_id"] != str(interaction.guild_id):
            await interaction.response.send_message("This key belongs to a different server.", ephemeral=True)
            return
        if int(row["used"]) == 1:
            await interaction.response.send_message("This key has already been used.", ephemeral=True)
            return

        expires_at = datetime.fromisoformat(row["expires_at"])
        if utc_now() > expires_at:
            await interaction.response.send_message("This key expired. Generate a new one.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return

        role = guild.get_role(ROLE_ID)
        if role is None:
            await interaction.response.send_message("Configured role not found.", ephemeral=True)
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("Member not found.", ephemeral=True)
            return

        role_expires_at = utc_now() + timedelta(minutes=ROLE_DURATION_MINUTES)

        try:
            await member.add_roles(role, reason=f"Key redeemed: {code}")
            mark_key_used(code, interaction.guild_id, role_expires_at)
        except discord.Forbidden:
            await interaction.response.send_message("Bot lacks permission to assign that role.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Access granted",
            description=f"You received {role.mention} for {ROLE_DURATION_MINUTES} minutes.",
            color=0x22C55E,
        )
        embed.add_field(name="Key", value=f"`{code}`", inline=True)
        embed.add_field(name="Expires", value=role_expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    bot.add_view(KeyPanel())
    try:
        synced = await bot.tree.sync(guild=TREE_GUILD)
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Sync failed: {e}")
    if not role_cleanup.is_running():
        role_cleanup.start()
    print(f"Logged in as {bot.user}")


@bot.tree.command(name="sendpanel", description="Send the key panel", guild=TREE_GUILD)
@app_commands.checks.has_permissions(administrator=True)
async def sendpanel(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"{BRAND_NAME} • How to access VIP",
        description=(
            "✨ Click **Generate Key**\n"
            "🔑 Open the page and create your private code\n"
            "📋 Copy the key\n"
            "🛡️ Click **Redeem Key** and paste it\n"
            f"🎉 If valid, you get the role for **{ROLE_DURATION_MINUTES} minutes**"
        ),
        color=0x8B5CF6,
    )
    embed.set_footer(text="One-time use • User-bound • Auto-removes after expiry")
    await interaction.channel.send(embed=embed, view=KeyPanel())
    await interaction.response.send_message("Panel sent.", ephemeral=True)


@tasks.loop(seconds=30)
async def role_cleanup():
    await bot.wait_until_ready()
    rows = get_expired_role_records()
    if not rows:
        return

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    role = guild.get_role(ROLE_ID)
    if role is None:
        return

    for row in rows:
        member = guild.get_member(int(row["user_id"]))
        if member and role in member.roles:
            try:
                await member.remove_roles(role, reason="Temporary key role expired")
            except discord.Forbidden:
                pass
        clear_role_expiry(row["code"])


def run_web():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


def main():
    init_db()
    Thread(target=run_web, daemon=True).start()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
