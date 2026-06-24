"""Async SQLite storage for per-guild settings and per-user balances/time."""
import os
import time

import aiosqlite

# Only these setting columns may be updated programmatically.
_ALLOWED_SETTING_KEYS = {
    "required_status",
    "reward_seconds",
    "coins_per_reward",
    "log_channel_id",
    "eligible_statuses",
}

# Only these user columns may be updated via upsert_user.
_ALLOWED_USER_KEYS = {
    "total_eligible_seconds",
    "coins",
    "rewards_count",
    "updated_at",
}


class Database:
    def __init__(self, path="bot_data.db"):
        self.path = path
        self._conn = None

    async def connect(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._create_tables()
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self):
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                required_status TEXT,
                reward_seconds REAL,
                coins_per_reward INTEGER,
                log_channel_id INTEGER,
                eligible_statuses TEXT
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER,
                user_id INTEGER,
                total_eligible_seconds REAL DEFAULT 0,
                coins INTEGER DEFAULT 0,
                rewards_count INTEGER DEFAULT 0,
                updated_at REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_leaderboards (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            )
            """
        )

    # ---------- settings ----------
    async def get_settings(self, guild_id, defaults):
        cur = await self._conn.execute(
            "SELECT * FROM settings WHERE guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        if row is None:
            await self._conn.execute(
                """INSERT INTO settings
                   (guild_id, required_status, reward_seconds, coins_per_reward,
                    log_channel_id, eligible_statuses)
                   VALUES (?,?,?,?,?,?)""",
                (
                    guild_id,
                    defaults["required_status"],
                    defaults["reward_seconds"],
                    defaults["coins_per_reward"],
                    defaults["log_channel_id"],
                    defaults["eligible_statuses"],
                ),
            )
            await self._conn.commit()
            out = dict(defaults)
            out["guild_id"] = guild_id
            return out
        return dict(row)

    async def update_setting(self, guild_id, key, value):
        if key not in _ALLOWED_SETTING_KEYS:
            raise ValueError(f"Illegal setting key: {key}")
        await self._conn.execute(
            f"UPDATE settings SET {key} = ? WHERE guild_id = ?", (value, guild_id)
        )
        await self._conn.commit()

    # ---------- users ----------
    async def get_user(self, guild_id, user_id):
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            now = time.time()
            await self._conn.execute(
                "INSERT INTO users (guild_id, user_id, updated_at) VALUES (?,?,?)",
                (guild_id, user_id, now),
            )
            await self._conn.commit()
            return {
                "guild_id": guild_id,
                "user_id": user_id,
                "total_eligible_seconds": 0.0,
                "coins": 0,
                "rewards_count": 0,
                "updated_at": now,
            }
        return dict(row)

    async def upsert_user(self, guild_id, user_id, **fields):
        for k in fields:
            if k not in _ALLOWED_USER_KEYS:
                raise ValueError(f"Illegal user key: {k}")
        await self.get_user(guild_id, user_id)  # ensure row exists
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [guild_id, user_id]
        await self._conn.execute(
            f"UPDATE users SET {cols} WHERE guild_id=? AND user_id=?", vals
        )
        await self._conn.commit()

    async def add_coins(self, guild_id, user_id, amount):
        u = await self.get_user(guild_id, user_id)
        new = max(0, u["coins"] + amount)
        await self.upsert_user(guild_id, user_id, coins=new)
        return new

    async def set_coins(self, guild_id, user_id, amount):
        new = max(0, amount)
        await self.upsert_user(guild_id, user_id, coins=new)
        return new

    async def leaderboard(self, guild_id, limit=10):
        cur = await self._conn.execute(
            "SELECT user_id, coins FROM users WHERE guild_id=? "
            "ORDER BY coins DESC, total_eligible_seconds DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    # ---------- live leaderboard ----------
    async def set_live_leaderboard(self, guild_id, channel_id, message_id):
        await self._conn.execute(
            "INSERT OR REPLACE INTO live_leaderboards "
            "(guild_id, channel_id, message_id) VALUES (?,?,?)",
            (guild_id, channel_id, message_id),
        )
        await self._conn.commit()

    async def get_live_leaderboard(self, guild_id):
        cur = await self._conn.execute(
            "SELECT guild_id, channel_id, message_id FROM live_leaderboards "
            "WHERE guild_id=?",
            (guild_id,),
        )
        return await cur.fetchone()

    async def get_all_live_leaderboards(self):
        cur = await self._conn.execute(
            "SELECT guild_id, channel_id, message_id FROM live_leaderboards"
        )
        return await cur.fetchall()

    async def clear_live_leaderboard(self, guild_id):
        await self._conn.execute(
            "DELETE FROM live_leaderboards WHERE guild_id=?", (guild_id,)
        )
        await self._conn.commit()
