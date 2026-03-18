import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# =========================
# CONFIG
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN nije postavljen.")

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ROLE_ID = int(os.getenv("ROLE_ID", "0"))
BOT_CHANNEL_ID = int(os.getenv("BOT_CHANNEL_ID", "0"))  # kanal za create keys / panel
LOG_CHANNEL_ID = 1483043753923973243  # logs kanal koji si dao

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://spicyvault.online")
LOCKR_URL = os.getenv("LOCKR_URL", "https://lockr.so/u6TypiAY")

DATABASE_PATH = os.getenv("DATABASE_PATH", "keys.db")
ROLE_DURATION_MINUTES = int(os.getenv("ROLE_DURATION_MINUTES", "60"))
KEY_EXPIRE_MINUTES = int(os.getenv("KEY_EXPIRE_MINUTES", "10"))
GENERATE_COOLDOWN_SECONDS = int(os.getenv("GENERATE_COOLDOWN_SECONDS", "60"))
PORT = int(os.getenv("PORT", "8000"))
BRAND_NAME = os.getenv("BRAND_NAME", "Spicy Vault")

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
        return conn.execute("SELECT * FROM keys WHERE code = ?", (code.upper(),)).fetchone()

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

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/key/generate")
def api_generate_key(payload: GenerateRequest, request: Request):
    if payload.guild_id != GUILD_ID:
        raise HTTPException(status_code=400, detail="Invalid guild.")

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

# =========================
# DISCORD BOT HELPERS
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
TREE_GUILD = discord.Object(id=GUILD_ID)

async def send_log_message(content: str) -> None:
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            return
    try:
        await channel.send(content)
    except Exception:
        pass

def parse_hex_color(value: str) -> int:
    value = value.strip().replace("#", "")
    if len(value) != 6:
        return 0x8B5CF6
    try:
        return int(value, 16)
    except ValueError:
        return 0x8B5CF6

def build_panel_embed(title: str, description: str, color_hex: str, image_url: str) -> discord.Embed:
    embed = discord.Embed(
        title=title or f"{BRAND_NAME} • How to access VIP",
        description=description or (
            "✨ Click **Generate Key**\n"
            "🔗 Open Lockr first\n"
            "🔑 Then open the key generator\n"
            "📋 Copy the key\n"
            "🛡️ Click **Redeem Key** and paste it\n"
            f"🎉 If valid, you get the role for **{ROLE_DURATION_MINUTES} minutes**"
        ),
        color=parse_hex_color(color_hex),
    )
    if image_url and image_url.strip():
        embed.set_image(url=image_url.strip())
    embed.set_footer(text="One-time use • User-bound • Auto-removes after expiry")
    return embed

# =========================
# PANEL BUILDER
# =========================

class PanelConfigModal(discord.ui.Modal, title="Customize Panel"):
    panel_title = discord.ui.TextInput(
        label="Embed Title",
        placeholder="Spicy Vault • How to access VIP",
        required=False,
        max_length=256
    )
    panel_description = discord.ui.TextInput(
        label="Embed Description",
        placeholder="Write your embed text here...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000
    )
    panel_color = discord.ui.TextInput(
        label="Embed Color HEX",
        placeholder="#8B5CF6",
        required=False,
        max_length=7
    )
    panel_image = discord.ui.TextInput(
        label="Image URL",
        placeholder="https://i.imgur.com/example.png",
        required=False,
        max_length=1000
    )
    target_channel_id = discord.ui.TextInput(
        label="Channel ID where panel will be sent",
        placeholder="1483228110358057131",
        required=True,
        max_length=30
    )

    async def on_submit(self, interaction: discord.Interaction):
        title = str(self.panel_title).strip()
        description = str(self.panel_description).strip()
        color_hex = str(self.panel_color).strip() or "#8B5CF6"
        image_url = str(self.panel_image).strip()
        channel_id_raw = str(self.target_channel_id).strip()

        if not channel_id_raw.isdigit():
            await interaction.response.send_message("Channel ID must be numbers only.", ephemeral=True)
            return

        channel_id = int(channel_id_raw)
        channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
        if channel is None:
            await interaction.response.send_message("Channel not found in this server.", ephemeral=True)
            return

        embed = build_panel_embed(title, description, color_hex, image_url)
        view = PanelPreviewView(
            author_id=interaction.user.id,
            target_channel_id=channel_id,
            title=title,
            description=description,
            color_hex=color_hex,
            image_url=image_url,
        )
        await interaction.response.send_message(
            content=f"Preview for <#{channel_id}>",
            embed=embed,
            view=view,
            ephemeral=True
        )

class PanelPreviewView(discord.ui.View):
    def __init__(self, author_id: int, target_channel_id: int, title: str, description: str, color_hex: str, image_url: str):
        super().__init__(timeout=900)
        self.author_id = author_id
        self.target_channel_id = target_channel_id
        self.title = title
        self.description = description
        self.color_hex = color_hex
        self.image_url = image_url

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This preview is not yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Save & Send", style=discord.ButtonStyle.success)
    async def save_and_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(self.target_channel_id) if interaction.guild else None
        if channel is None:
            await interaction.response.send_message("Target channel not found.", ephemeral=True)
            return

        embed = build_panel_embed(self.title, self.description, self.color_hex, self.image_url)
        await channel.send(embed=embed, view=KeyPanel())
        await interaction.response.send_message(f"Panel sent to <#{self.target_channel_id}>.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Panel send cancelled.", ephemeral=True)

# =========================
# KEY PANEL / LOCKR / REDEEM
# =========================

class ContinueView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id

    @discord.ui.button(label="Open Lockr", style=discord.ButtonStyle.link, url=LOCKR_URL)
    async def open_lockr(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="Continue to Key Generator", style=discord.ButtonStyle.primary)
    async def continue_to_keygen(self, interaction: discord.Interaction, button: discord.ui.Button):
        url = f"{PUBLIC_BASE_URL}/keygenerator?user_id={self.user_id}&guild_id={self.guild_id}"

        embed = discord.Embed(
            title=f"{BRAND_NAME} • Key Generator",
            description=(
                "1. Opened Lockr\n"
                "2. Now click the button below\n"
                "3. Generate your 6-character key\n"
                "4. Come back and redeem it"
            ),
            color=0x8B5CF6
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Key Generator", style=discord.ButtonStyle.link, url=url))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class KeyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Key", style=discord.ButtonStyle.primary, custom_id="key_generate")
    async def generate_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=f"{BRAND_NAME} • How to access VIP",
            description=(
                "✨ Click **Open Lockr**\n"
                "🔑 Then click **Continue to Key Generator**\n"
                "📋 Generate and copy your private code\n"
                "🛡️ Click **Redeem Key** and paste it\n"
                f"🎉 If valid, you get the role for **{ROLE_DURATION_MINUTES} minutes**"
            ),
            color=0x8B5CF6,
        )
        embed.set_footer(text="One-time use • User-bound • Auto-removes after expiry")
        await interaction.response.send_message(
            embed=embed,
            view=ContinueView(interaction.user.id, interaction.guild_id),
            ephemeral=True
        )

    @discord.ui.button(label="Redeem Key", style=discord.ButtonStyle.success, custom_id="key_redeem")
    async def redeem_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RedeemModal())

class RedeemModal(discord.ui.Modal, title="Redeem your key"):
    key_input = discord.ui.TextInput(
        label="Enter your key",
        placeholder="Example: ARM36V",
        min_length=6,
        max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        code = str(self.key_input).strip().upper()
        row = get_key(code)

        if not row:
            await send_log_message(f"❌ {interaction.user.mention} tried redeeming invalid key `{code}` in <#{interaction.channel_id}>.")
            await interaction.response.send_message("Your key was invalid.", ephemeral=True)
            return

        if row["user_id"] != str(interaction.user.id):
            await send_log_message(f"❌ {interaction.user.mention} tried using key `{code}` that does not belong to them.")
            await interaction.response.send_message("This key was not generated for your account.", ephemeral=True)
            return

        if row["guild_id"] != str(interaction.guild_id):
            await send_log_message(f"❌ {interaction.user.mention} tried using key `{code}` in the wrong server.")
            await interaction.response.send_message("This key belongs to a different server.", ephemeral=True)
            return

        if int(row["used"]) == 1:
            await send_log_message(f"❌ {interaction.user.mention} tried redeeming already used key `{code}`.")
            await interaction.response.send_message("This key has already been used.", ephemeral=True)
            return

        expires_at = datetime.fromisoformat(row["expires_at"])
        if utc_now() > expires_at:
            await send_log_message(f"⌛ {interaction.user.mention} tried redeeming expired key `{code}`.")
            await interaction.response.send_message(
                f"Your key has expired. Generate a new one in <#{BOT_CHANNEL_ID}>.",
                ephemeral=True
            )
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

        if role in member.roles:
            await interaction.response.send_message("You already have that role.", ephemeral=True)
            return

        role_expires_at = utc_now() + timedelta(minutes=ROLE_DURATION_MINUTES)

        try:
            await member.add_roles(role, reason=f"Key redeemed: {code}")
            mark_key_used(code, interaction.guild_id, role_expires_at)
        except discord.Forbidden:
            await interaction.response.send_message("Bot lacks permission to assign that role.", ephemeral=True)
            return

        await send_log_message(
            f"✅ {interaction.user.mention} redeemed key `{code}` successfully and has been granted {role.mention} for {ROLE_DURATION_MINUTES} minutes."
        )

        embed = discord.Embed(
            title="Access granted",
            description=f"You have been granted {role.mention} for {ROLE_DURATION_MINUTES} minutes.",
            color=0x22C55E,
        )
        embed.add_field(name="Key", value=f"`{code}`", inline=True)
        embed.add_field(name="Expires", value=role_expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================
# EVENTS / COMMANDS / TASKS
# =========================

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

@bot.tree.command(name="sendpanel", description="Customize and send the key panel", guild=TREE_GUILD)
@app_commands.checks.has_permissions(administrator=True)
async def sendpanel(interaction: discord.Interaction):
    await interaction.response.send_modal(PanelConfigModal())

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
        code = row["code"]

        if member and role in member.roles:
            try:
                await member.remove_roles(role, reason="Temporary key role expired")
            except discord.Forbidden:
                pass

            await send_log_message(
                f"⌛ {member.mention}'s temporary role has expired. Key `{code}` is finished."
            )

            create_keys_channel = guild.get_channel(BOT_CHANNEL_ID)
            if create_keys_channel:
                try:
                    await create_keys_channel.send(
                        f"{member.mention} your key has expired. Generate a new one in <#{BOT_CHANNEL_ID}>."
                    )
                except Exception:
                    pass

        clear_role_expiry(code)

def run_web():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

def main():
    init_db()
    Thread(target=run_web, daemon=True).start()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
