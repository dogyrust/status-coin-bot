"""Authenticated HTTP API exposing coin balances to the gambling site.

The bot remains the single source of truth for coin balances. The casino
website calls these endpoints (server-side, with the shared API key) to read a
player's balance and to apply the result of a bet.

Endpoints (all under the configured guild):
  GET  /health                      -> {"ok": true}
  GET  /api/coins/balance?user_id=  -> {"user_id", "coins"}
  GET  /api/coins/leaderboard?limit= -> {"leaderboard": [{"user_id", "coins"}, ...]}
  POST /api/coins/adjust            -> {"user_id", "coins", "applied"}
       body: {"user_id": <int>, "delta": <int>}

All /api/* routes require the header `X-API-Key: <CASINO_API_KEY>`.
"""
import hmac
import logging

from aiohttp import web

import config

log = logging.getLogger("status-coin-bot.web")


def _unauthorized():
    return web.json_response({"error": "unauthorized"}, status=401)


def _require_key(request):
    """Return True when the request carries the correct API key."""
    provided = request.headers.get("X-API-Key", "")
    expected = config.CASINO_API_KEY
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _parse_user_id(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


class CoinApiServer:
    """Runs an aiohttp web server alongside the Discord bot."""

    def __init__(self, bot):
        self.bot = bot
        self._runner = None

    @property
    def guild_id(self):
        return config.CASINO_API_GUILD_ID

    async def start(self):
        if not config.CASINO_API_KEY:
            log.info("CASINO_API_KEY not set - coin API disabled.")
            return
        if not self.guild_id:
            log.warning(
                "CASINO_API_KEY is set but no GUILD_ID/CASINO_API_GUILD_ID; "
                "coin API disabled."
            )
            return

        app = web.Application()
        app.add_routes(
            [
                web.get("/health", self.handle_health),
                web.get("/api/coins/balance", self.handle_balance),
                web.get("/api/coins/leaderboard", self.handle_leaderboard),
                web.post("/api/coins/adjust", self.handle_adjust),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", config.CASINO_API_PORT)
        await site.start()
        log.info("Coin API listening on port %s", config.CASINO_API_PORT)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ----- handlers -----
    async def handle_health(self, request):
        return web.json_response({"ok": True})

    async def handle_balance(self, request):
        if not _require_key(request):
            return _unauthorized()
        user_id = _parse_user_id(request.query.get("user_id"))
        if user_id is None:
            return web.json_response({"error": "invalid user_id"}, status=400)
        user = await self.bot.db.get_user(self.guild_id, user_id)
        return web.json_response({"user_id": user_id, "coins": user["coins"]})

    async def handle_leaderboard(self, request):
        if not _require_key(request):
            return _unauthorized()
        try:
            limit = int(request.query.get("limit", 10))
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid limit"}, status=400)
        limit = max(1, min(limit, 50))
        rows = await self.bot.db.leaderboard(self.guild_id, limit)
        return web.json_response(
            {
                "leaderboard": [
                    {"user_id": row["user_id"], "coins": row["coins"]}
                    for row in rows
                ]
            }
        )

    async def handle_adjust(self, request):
        if not _require_key(request):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)

        user_id = _parse_user_id(body.get("user_id"))
        if user_id is None:
            return web.json_response({"error": "invalid user_id"}, status=400)

        delta = body.get("delta")
        if not isinstance(delta, int) or isinstance(delta, bool):
            return web.json_response({"error": "delta must be an integer"}, status=400)

        ok, balance = await self.bot.db.try_adjust_coins(
            self.guild_id, user_id, delta
        )
        if not ok:
            return web.json_response(
                {"error": "insufficient_balance", "coins": balance},
                status=409,
            )
        return web.json_response(
            {"user_id": user_id, "coins": balance, "applied": delta}
        )
