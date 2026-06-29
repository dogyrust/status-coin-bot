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


def _get_id_set(name, default):
    raw = os.getenv(name)
    if not raw:
        return set(default)
    ids = {int(p) for p in raw.replace(",", " ").split() if p.strip().isdigit()}
    return ids or set(default)


# --- Required ---
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Optional: set to your server (guild) ID for INSTANT slash-command updates.
# Leave blank to register commands globally (can take up to ~1 hour the first time).
GUILD_ID = _get_int("GUILD_ID")

# Members with THIS role (or any higher role in the hierarchy) may run
# /check on other members. Override with the ADMIN_ROLE_ID env var.
ADMIN_ROLE_ID = _get_int("ADMIN_ROLE_ID", 1323908939074502684)

# ONLY these user IDs may use the /admin commands. Override with ADMIN_USER_IDS
# (space- or comma-separated list of user IDs).
ADMIN_USER_IDS = _get_id_set(
    "ADMIN_USER_IDS",
    [424905705909387265, 610910145668448256, 646442234618576916],
)

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

# --- Casino / coin sync HTTP API ---
# A small authenticated HTTP server that lets the gambling site read and
# adjust coin balances. Disabled unless CASINO_API_KEY is set.
CASINO_API_KEY = os.getenv("CASINO_API_KEY", "").strip()
# Port to listen on. Railway/most hosts inject PORT; fall back to 8080.
CASINO_API_PORT = _get_int("CASINO_API_PORT") or _get_int("PORT", 8080)
# Guild whose balances the API serves. Falls back to GUILD_ID.
CASINO_API_GUILD_ID = _get_int("CASINO_API_GUILD_ID") or GUILD_ID

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
    {"account_type": "arc_0_99_hours", "label": "Arc 0-99 hours", "cost": 1},
    {"account_type": "arc_100_200_hours", "label": "Arc 100-200 hours", "cost": 3},
    {"account_type": "arc_200_plus_hours", "label": "Arc 200+ hours", "cost": 5},
    {"account_type": "cs2_prime", "label": "CS2 Prime", "cost": 1},
    {"account_type": "cs2_premier", "label": "CS2 Premier", "cost": 1},
    {"account_type": "cs2_10_15k_elo", "label": "CS2 10-15k ELO", "cost": 2},
    {"account_type": "cs2_15_20k_elo", "label": "CS2 15-20k ELO", "cost": 3},
]
try:
    STORE_TIERS = json.loads(os.getenv("STORE_TIERS", "")) or DEFAULT_STORE_TIERS
except (ValueError, TypeError):
    STORE_TIERS = DEFAULT_STORE_TIERS
