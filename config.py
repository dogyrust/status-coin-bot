"""Configuration loader. Reads from environment / .env file.

Most values here are just DEFAULTS. Per-server settings (required status text,
reward interval, coins per reward, log channel) are stored in the database and
can be changed at runtime with the /admin commands.
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(name, default=None):
    raw = os.getenv(name)
    if raw and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return default


# --- Required ---
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Optional: set to your server (guild) ID for INSTANT slash-command updates.
# Leave blank to register commands globally (can take up to ~1 hour the first time).
GUILD_ID = _get_int("GUILD_ID")

# --- Default reward settings (changeable per-server via /admin) ---
DEFAULT_REQUIRED_STATUS = os.getenv("REQUIRED_STATUS", "$1.20 Rust: nfaccount.com")
DEFAULT_REWARD_HOURS = float(os.getenv("REWARD_HOURS", "48"))
DEFAULT_COINS_PER_REWARD = _get_int("COINS_PER_REWARD", 1)
DEFAULT_LOG_CHANNEL_ID = _get_int("LOG_CHANNEL_ID")
DEFAULT_ELIGIBLE_STATUSES = os.getenv("ELIGIBLE_STATUSES", "online,dnd")

# --- Cosmetic ---
COIN_NAME = os.getenv("COIN_NAME", "coin")
COIN_EMOJI = os.getenv("COIN_EMOJI", "\U0001FA99")  # 🪙
EMBED_COLOR = int(os.getenv("EMBED_COLOR", "0x33C2FF"), 16)  # icy blue

# --- Internals ---
# How often (seconds) the bot credits online time and checks for rewards.
TICK_SECONDS = _get_int("TICK_SECONDS", 60)

# The bot's OWN displayed status. Defaults to the same advertising text.
BOT_STATUS_TEXT = os.getenv("BOT_STATUS_TEXT", DEFAULT_REQUIRED_STATUS)
# One of: custom, playing, watching, listening, competing, streaming
BOT_ACTIVITY_TYPE = os.getenv("BOT_ACTIVITY_TYPE", "custom").lower()
STREAM_URL = os.getenv("STREAM_URL", "https://www.twitch.tv/discord")

DB_PATH = os.getenv("DB_PATH", "bot_data.db")

# --- NFA Resell API (powers /store) ---
# Set NFA_API_KEY as an environment variable / Railway secret. Never hardcode it.
NFA_API_KEY = os.getenv("NFA_API_KEY", "").strip()
NFA_API_BASE = os.getenv("NFA_API_BASE", "https://nfa-api.acode.ing").rstrip("/")

# --- Store products (priced in the bot's coins, not USD) ---
# Override with a STORE_TIERS env var (JSON list) if you want to change them.
DEFAULT_STORE_TIERS = [
    {"account_type": "rust_0_250_hours", "label": "Rust 0-250 hours (Base)", "cost": 1},
    {"account_type": "rust_500_1000_hours", "label": "Rust 500-1000 hours", "cost": 3},
    {"account_type": "rust_3000_7000_hours", "label": "Rust 3000-7000 hours", "cost": 4},
]
try:
    STORE_TIERS = json.loads(os.getenv("STORE_TIERS", "")) or DEFAULT_STORE_TIERS
except (ValueError, TypeError):
    STORE_TIERS = DEFAULT_STORE_TIERS
