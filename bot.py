"""Status-Coin Bot.

Tracks members' Discord presence. A member earns coins for accumulated time
spent ONLINE (or DND) while displaying a required custom-status text.
By default: 1 coin per 48h of eligible online time.

Only time while online/dnd AND showing the status counts toward the 48h.
"""
import logging
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from db import Database
from web_api import CoinApiServer

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("status-coin-bot")

intents = discord.Intents.default()
intents.members = True      # privileged: Server Members Intent
intents.presences = True    # privileged: Presence Intent
intents.guilds = True

DEFAULTS = {
    "required_status": config.DEFAULT_REQUIRED_STATUS,
    "reward_seconds": config.DEFAULT_REWARD_HOURS * 3600.0,
    "coins_per_reward": config.DEFAULT_COINS_PER_REWARD,
    "log_channel_id": config.DEFAULT_LOG_CHANNEL_ID,
    "eligible_statuses": config.DEFAULT_ELIGIBLE_STATUSES,
}

_STATUS_MAP = {
    "online": discord.Status.online,
    "dnd": discord.Status.dnd,
    "idle": discord.Status.idle,
    "offline": discord.Status.offline,
}


# --------------------------- helpers ---------------------------
def parse_statuses(raw):
    out = set()
    for part in (raw or "").split(","):
        s = _STATUS_MAP.get(part.strip().lower())
        if s:
            out.add(s)
    return out or {discord.Status.online, discord.Status.dnd}


def get_custom_status_text(member):
    for activity in getattr(member, "activities", ()) or ():
        if isinstance(activity, discord.CustomActivity):
            return activity.name or ""
    return ""


def human_duration(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def build_activity():
    text = config.BOT_STATUS_TEXT
    t = config.BOT_ACTIVITY_TYPE
    if t == "playing":
        return discord.Game(name=text)
    if t == "watching":
        return discord.Activity(type=discord.ActivityType.watching, name=text)
    if t == "listening":
        return discord.Activity(type=discord.ActivityType.listening, name=text)
    if t == "competing":
        return discord.Activity(type=discord.ActivityType.competing, name=text)
    if t == "streaming":
        return discord.Streaming(name=text, url=config.STREAM_URL)
    return discord.CustomActivity(name=text)


def extract_stock(stock, account_type):
    """Pull a numeric stock count for an account type from the /stock payload."""
    value = stock.get(account_type) if isinstance(stock, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("stock", "count", "available", "amount", "qty"):
            v = value.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
    return None


def can_check_others(member):
    """True if member may run /check on someone else (admin role or higher)."""
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    if member.id == guild.owner_id:
        return True
    role = guild.get_role(config.ADMIN_ROLE_ID) if config.ADMIN_ROLE_ID else None
    if role is None:
        # Configured role missing? fall back to the Manage Server permission.
        return member.guild_permissions.manage_guild
    top = getattr(member, "top_role", None)
    return top is not None and top.position >= role.position


# --------------------------- bot ---------------------------
class StatusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(config.DB_PATH)
        # (guild_id, user_id) -> unix timestamp when they became eligible.
        self.eligible_since = {}
        self._settings_cache = {}
        self.session = None
        self._store_cache = None
        self._buying = set()
        self.coin_api = CoinApiServer(self)

    async def setup_hook(self):
        await self.db.connect()
        self.session = aiohttp.ClientSession()
        await self.coin_api.start()
        self.tree.add_command(AdminGroup(self))
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", config.GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to 1h first time)")
        self.reward_loop.start()
        self.leaderboard_loop.start()

    # ----- settings cache -----
    async def get_guild_settings(self, guild_id):
        cached = self._settings_cache.get(guild_id)
        if cached:
            return cached
        s = await self.db.get_settings(guild_id, DEFAULTS)
        self._settings_cache[guild_id] = s
        return s

    def invalidate_settings(self, guild_id):
        self._settings_cache.pop(guild_id, None)

    # ----- NFA Resell API (powers /store) -----
    async def nfa_get(self, path, params=None):
        url = f"{config.NFA_API_BASE}{path}"
        headers = {"X-API-Key": config.NFA_API_KEY}
        timeout = aiohttp.ClientTimeout(total=20)
        async with self.session.get(
            url, params=params, headers=headers, timeout=timeout
        ) as resp:
            data = await resp.json(content_type=None)
            return resp.status, data

    async def nfa_post(self, path, payload, timeout=30):
        url = f"{config.NFA_API_BASE}{path}"
        headers = {"X-API-Key": config.NFA_API_KEY}
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with self.session.post(
            url, json=payload, headers=headers, timeout=client_timeout
        ) as resp:
            data = await resp.json(content_type=None)
            return resp.status, data

    async def get_store(self):
        """Return (accounts: {name: price}, stock: {name: count}); cached 2 min
        to protect the NFA rate limit."""
        now = time.time()
        if self._store_cache and now - self._store_cache[0] < 120:
            return self._store_cache[1]
        _, acc = await self.nfa_get("/api/v1/accounts")
        _, stk = await self.nfa_get("/api/v1/stock")
        accounts = acc.get("accounts", {}) if isinstance(acc, dict) else {}
        stock = stk.get("stock", {}) if isinstance(stk, dict) else {}
        result = (accounts, stock)
        self._store_cache = (now, result)
        return result

    # ----- eligibility + time accounting -----
    def is_eligible(self, member, settings):
        if member is None or member.bot:
            return False
        if member.status not in parse_statuses(settings["eligible_statuses"]):
            return False
        text = get_custom_status_text(member)
        if not text:
            return False
        return settings["required_status"].strip().lower() in text.lower()

    def mark_eligible(self, guild_id, user_id, now=None):
        key = (guild_id, user_id)
        if key not in self.eligible_since:
            self.eligible_since[key] = now or time.time()

    async def flush_user(self, guild_id, user_id, now=None):
        """Move accrued eligible time into the DB; keep the timer running."""
        now = now or time.time()
        key = (guild_id, user_id)
        since = self.eligible_since.get(key)
        if since is None:
            return
        elapsed = now - since
        self.eligible_since[key] = now
        if elapsed <= 0:
            return
        u = await self.db.get_user(guild_id, user_id)
        await self.db.upsert_user(
            guild_id,
            user_id,
            total_eligible_seconds=u["total_eligible_seconds"] + elapsed,
            updated_at=now,
        )

    async def mark_ineligible(self, guild_id, user_id, now=None):
        key = (guild_id, user_id)
        if key in self.eligible_since:
            await self.flush_user(guild_id, user_id, now)
            self.eligible_since.pop(key, None)

    async def process_rewards(self, guild_id, user_id, settings):
        """Grant coins for any newly-completed reward intervals. Returns gained."""
        reward_seconds = settings["reward_seconds"]
        if reward_seconds <= 0:
            return 0
        u = await self.db.get_user(guild_id, user_id)
        target = int(u["total_eligible_seconds"] // reward_seconds)
        if target > u["rewards_count"]:
            diff = target - u["rewards_count"]
            gained = diff * int(settings["coins_per_reward"])
            await self.db.upsert_user(
                guild_id,
                user_id,
                coins=u["coins"] + gained,
                rewards_count=target,
            )
            return gained
        return 0

    async def announce_reward(self, guild, user_id, gained, settings):
        u = await self.db.get_user(guild.id, user_id)
        log.info("Rewarded %s coin(s) to %s in guild %s", gained, user_id, guild.id)
        chan_id = settings.get("log_channel_id")
        if not chan_id:
            return
        channel = guild.get_channel(int(chan_id))
        if channel is None:
            return
        member = guild.get_member(user_id)
        who = member.mention if member else f"<@{user_id}>"
        embed = discord.Embed(
            title=f"{config.COIN_EMOJI} Reward Earned!",
            description=(
                f"{who} earned **{gained} {config.COIN_NAME}(s)** for staying "
                f"online with the required status.\n"
                f"New balance: **{u['coins']} {config.COIN_NAME}(s)**"
            ),
            color=config.EMBED_COLOR,
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ----- background loop -----
    @tasks.loop(seconds=config.TICK_SECONDS)
    async def reward_loop(self):
        now = time.time()
        for guild in self.guilds:
            settings = await self.get_guild_settings(guild.id)
            keys = [k for k in list(self.eligible_since.keys()) if k[0] == guild.id]
            for (gid, uid) in keys:
                await self.flush_user(gid, uid, now)
                gained = await self.process_rewards(gid, uid, settings)
                if gained > 0:
                    await self.announce_reward(guild, uid, gained, settings)

    @reward_loop.before_loop
    async def _before_loop(self):
        await self.wait_until_ready()

    async def update_live_leaderboards(self):
        for row in await self.db.get_all_live_leaderboards():
            guild = self.get_guild(row["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                await self.db.clear_live_leaderboard(row["guild_id"])
                continue
            embed = await build_leaderboard_embed(guild, live=True)
            try:
                await channel.get_partial_message(row["message_id"]).edit(embed=embed)
            except discord.NotFound:
                await self.db.clear_live_leaderboard(row["guild_id"])
            except discord.HTTPException as exc:
                log.warning("Leaderboard update failed: %s", exc)

    @tasks.loop(seconds=60)
    async def leaderboard_loop(self):
        try:
            await self.update_live_leaderboards()
        except Exception as exc:  # noqa: BLE001
            log.warning("leaderboard_loop error: %s", exc)

    @leaderboard_loop.before_loop
    async def _before_leaderboard_loop(self):
        await self.wait_until_ready()

    # ----- events -----
    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        try:
            await self.change_presence(
                activity=build_activity(), status=discord.Status.online
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not set bot presence: %s", exc)

        now = time.time()
        for guild in self.guilds:
            settings = await self.get_guild_settings(guild.id)
            for member in guild.members:
                if member.bot:
                    continue
                if self.is_eligible(member, settings):
                    self.mark_eligible(guild.id, member.id, now)
                else:
                    await self.mark_ineligible(guild.id, member.id, now)
        log.info("Initialised tracking for %d eligible member(s)", len(self.eligible_since))

    async def on_presence_update(self, before, after):
        member = after
        if member.bot or member.guild is None:
            return
        guild_id = member.guild.id
        settings = await self.get_guild_settings(guild_id)
        now = time.time()
        eligible = self.is_eligible(member, settings)
        key = (guild_id, member.id)
        was = key in self.eligible_since

        if eligible and not was:
            self.mark_eligible(guild_id, member.id, now)
        elif not eligible and was:
            await self.mark_ineligible(guild_id, member.id, now)
        elif eligible and was:
            await self.flush_user(guild_id, member.id, now)
            gained = await self.process_rewards(guild_id, member.id, settings)
            if gained > 0:
                await self.announce_reward(member.guild, member.id, gained, settings)

    async def on_member_remove(self, member):
        self.eligible_since.pop((member.guild.id, member.id), None)

    async def close(self):
        now = time.time()
        for (gid, uid) in list(self.eligible_since.keys()):
            await self.flush_user(gid, uid, now)
        await self.coin_api.stop()
        await self.db.close()
        if self.session:
            await self.session.close()
        await super().close()


bot = StatusBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"\u23F3 Slow down \u2014 try again in {error.retry_after:.0f}s."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass
        return
    if isinstance(error, app_commands.CheckFailure):
        # Authorisation message was already sent in the check itself.
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    "You can't use that command.", ephemeral=True
                )
            except discord.HTTPException:
                pass
        return
    log.exception("Unhandled app command error: %s", error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("Something went wrong.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "Something went wrong.", ephemeral=True
            )
    except discord.HTTPException:
        pass


async def live_total(guild_id, user_id, settings):
    """DB total plus any not-yet-flushed live time."""
    u = await bot.db.get_user(guild_id, user_id)
    total = u["total_eligible_seconds"]
    since = bot.eligible_since.get((guild_id, user_id))
    if since:
        total += time.time() - since
    reward_seconds = settings["reward_seconds"]
    into = total % reward_seconds if reward_seconds else 0
    remaining = (reward_seconds - into) if reward_seconds else 0
    pct = (into / reward_seconds * 100) if reward_seconds else 0
    return u, total, remaining, pct


# --------------------------- user commands ---------------------------
@bot.tree.command(name="balance", description="Check your coin balance and progress")
@app_commands.guild_only()
@app_commands.describe(user="Member to check (defaults to you)")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    settings = await bot.get_guild_settings(interaction.guild_id)
    u, total, remaining, pct = await live_total(interaction.guild_id, user.id, settings)
    embed = discord.Embed(
        title=f"{config.COIN_EMOJI} {user.display_name}'s Balance",
        color=config.EMBED_COLOR,
    )
    embed.add_field(name="Coins", value=f"**{u['coins']}** {config.COIN_NAME}(s)", inline=True)
    embed.add_field(name="Rewards earned", value=str(u["rewards_count"]), inline=True)
    embed.add_field(name="Total online time", value=human_duration(total), inline=False)
    embed.add_field(
        name="Next coin in",
        value=f"{human_duration(remaining)}  ({pct:.1f}% there)",
        inline=False,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="check", description="Check if you (or a member) currently qualify to earn coins")
@app_commands.guild_only()
@app_commands.describe(user="Member to check (defaults to you)")
async def check(interaction: discord.Interaction, user: discord.Member = None):
    if user is not None and not can_check_others(interaction.user):
        await interaction.response.send_message(
            "You need the admin role (or higher) to check another member. "
            "Use `/check` on its own to check yourself.",
            ephemeral=True,
        )
        return
    target = user or interaction.user
    # Re-fetch from the guild cache so we read live presence (status + custom
    # activity); the member resolved straight from a slash command can lack it.
    member = interaction.guild.get_member(target.id) or target
    settings = await bot.get_guild_settings(interaction.guild_id)
    statuses = parse_statuses(settings["eligible_statuses"])
    status_ok = member.status in statuses
    text = get_custom_status_text(member)
    text_ok = settings["required_status"].strip().lower() in (text or "").lower()
    eligible = status_ok and text_ok and not member.bot
    is_self = member.id == interaction.user.id
    embed = discord.Embed(
        title=f"Eligibility Check — {member.display_name}",
        color=discord.Color.green() if eligible else discord.Color.red(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="Online status",
        value=f"{'✅' if status_ok else '❌'} `{member.status}` "
        f"(need one of: {settings['eligible_statuses']})",
        inline=False,
    )
    embed.add_field(
        name="Required status text",
        value=f"{'✅' if text_ok else '❌'} must contain: `{settings['required_status']}`\n"
        f"status shown: `{text or '(none set)'}`",
        inline=False,
    )
    if eligible:
        earning = "✅ **Yes** — keep it up!"
    elif is_self:
        earning = "❌ **No** — fix the above"
    else:
        earning = "❌ **No** — they don't qualify right now"
    embed.add_field(name="Currently earning?", value=earning, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def build_leaderboard_embed(guild, limit=10, live=False):
    rows = await bot.db.leaderboard(guild.id, limit)
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = []
    for i, row in enumerate(rows):
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        prefix = medals[i] if i < 3 else f"`#{i + 1}`"
        lines.append(f"{prefix} **{name}** \u2014 {row['coins']} {config.COIN_NAME}(s)")
    embed = discord.Embed(
        title=f"{config.COIN_EMOJI} Leaderboard",
        description="\n".join(lines) if lines else "No one has earned coins yet.",
        color=config.EMBED_COLOR,
        timestamp=discord.utils.utcnow() if live else None,
    )
    if live:
        embed.set_footer(text="Live \u2022 updates every minute")
    return embed


@bot.tree.command(name="leaderboard", description="Top coin holders in this server")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    embed = await build_leaderboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="pay", description="Send some of your coins to another member")
@app_commands.guild_only()
@app_commands.describe(user="Recipient", amount="How many coins to send")
async def pay(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    if user.id == interaction.user.id or user.bot:
        await interaction.response.send_message("Invalid recipient.", ephemeral=True)
        return
    sender = await bot.db.get_user(interaction.guild_id, interaction.user.id)
    if sender["coins"] < amount:
        await interaction.response.send_message(
            "You don't have enough coins.", ephemeral=True
        )
        return
    await bot.db.add_coins(interaction.guild_id, interaction.user.id, -amount)
    await bot.db.add_coins(interaction.guild_id, user.id, amount)
    await interaction.response.send_message(
        f"{config.COIN_EMOJI} {interaction.user.mention} sent **{amount}** "
        f"{config.COIN_NAME}(s) to {user.mention}!"
    )


@bot.tree.command(name="howitworks", description="How to earn coins")
@app_commands.guild_only()
async def howitworks(interaction: discord.Interaction):
    s = await bot.get_guild_settings(interaction.guild_id)
    hours = s["reward_seconds"] / 3600
    embed = discord.Embed(
        title="How to Earn Coins",
        color=config.EMBED_COLOR,
        description=(
            f"**1.** Set your Discord **custom status** to include:\n"
            f"> `{s['required_status']}`\n\n"
            f"**2.** Stay **{s['eligible_statuses']}** (i.e. actually online).\n\n"
            f"**3.** For every **{hours:g} hours** of online time *with the status*, "
            f"you earn **{s['coins_per_reward']} {config.COIN_NAME}(s)**.\n\n"
            f"Only time while you're online **and** showing the status counts. "
            f"Use `/check` to confirm you're set up, and `/balance` to track progress."
        ),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="store", description="Browse the account shop (prices are in coins)")
@app_commands.guild_only()
async def store(interaction: discord.Interaction):
    await interaction.response.defer()
    stock = {}
    if config.NFA_API_KEY:
        try:
            _, stock = await bot.get_store()
        except Exception as exc:  # noqa: BLE001
            log.warning("Store stock fetch failed: %s", exc)
    lines = []
    for tier in config.STORE_TIERS:
        at = tier.get("account_type", "")
        label = tier.get("label", at)
        cost = tier.get("cost", 0)
        count = extract_stock(stock, at)
        if count is None:
            dot, stock_str = "\u26AA", "stock: n/a"
        elif count > 0:
            dot, stock_str = "\U0001F7E2", f"{count} in stock"
        else:
            dot, stock_str = "\U0001F534", "out of stock"
        lines.append(
            f"{dot} **{label}**\n"
            f"`{at}` \u2014 **{cost}** {config.COIN_EMOJI} {config.COIN_NAME}(s) "
            f"\u00b7 {stock_str}"
        )
    embed = discord.Embed(
        title="\U0001F6D2 Account Store",
        description="\n\n".join(lines) if lines else "No products configured.",
        color=config.EMBED_COLOR,
    )
    embed.set_footer(text=f"See your {config.COIN_NAME}s with /balance \u00b7 stock is live")
    await interaction.followup.send(embed=embed)


_STORE_CHOICES = [
    app_commands.Choice(
        name=f"{t.get('label', t['account_type'])} ({t.get('cost', 0)} {config.COIN_NAME})"[:100],
        value=t["account_type"],
    )
    for t in config.STORE_TIERS
][:25]


async def _safe_followup(interaction, **kwargs):
    try:
        await interaction.followup.send(**kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("followup failed: %s", exc)
        return False


ACTIVATION_URL = "https://nordicnfas.com/"


async def _deliver_key(interaction: discord.Interaction, tier, key):
    """DM the buyer their key and post a public purchase announcement; never raises."""
    label = tier.get("label", tier["account_type"])
    content = (
        f"\U0001F389 **{label}** \u2014 here's your key!\n\n"
        f"**Your key:** ||`{key}`||\n\n"
        f"**How to use it:**\n"
        f"1. Go to {ACTIVATION_URL}\n"
        f"2. Enter your key there to activate and download your account.\n\n"
        f"Keep this key private \u2014 treat it like cash."
    )

    dm_ok = False
    try:
        dm = await interaction.user.create_dm()
        await dm.send(content=content)
        dm_ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("DM delivery failed: %s", exc)

    # Public announcement everyone in the channel can see (the key is NOT shown).
    if interaction.channel is not None:
        announce = discord.Embed(
            title=f"{config.COIN_EMOJI} New purchase!",
            description=f"{interaction.user.mention} just bought **{label}** "
            f"for **{tier.get('cost', 0)}** {config.COIN_NAME}(s)!",
            color=config.EMBED_COLOR,
        )
        try:
            await interaction.channel.send(embed=announce)
        except discord.HTTPException as exc:
            log.warning("public announce failed: %s", exc)

    # Private confirmation to the buyer (also resolves the ephemeral interaction).
    if dm_ok:
        await _safe_followup(
            interaction,
            content="\u2705 Purchase complete \u2014 check your **DMs** for your key!",
            ephemeral=True,
        )
    else:
        await _safe_followup(
            interaction,
            content="\u2705 Purchase complete! I couldn't DM you (are your DMs open?), "
            "so here's your key privately:\n\n" + content,
            ephemeral=True,
        )


@bot.tree.command(name="buy", description="Spend coins to receive an account key")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 120.0, key=lambda i: i.user.id)
@app_commands.choices(product=_STORE_CHOICES)
@app_commands.describe(product="Which account to buy")
async def buy(interaction: discord.Interaction, product: app_commands.Choice[str]):
    account_type = product.value
    tier = next((t for t in config.STORE_TIERS if t["account_type"] == account_type), None)
    if tier is None:
        await interaction.response.send_message("Unknown product.", ephemeral=True)
        return
    if not config.NFA_API_KEY:
        await interaction.response.send_message(
            "The store isn't configured yet (an admin must set `NFA_API_KEY`).",
            ephemeral=True,
        )
        return

    cost = int(tier.get("cost", 0))
    guild_id, uid = interaction.guild_id, interaction.user.id
    key = (guild_id, uid)
    if key in bot._buying:
        await interaction.response.send_message(
            "You already have a purchase in progress \u2014 please wait.", ephemeral=True
        )
        return

    u = await bot.db.get_user(guild_id, uid)
    if u["coins"] < cost:
        await interaction.response.send_message(
            f"You need **{cost}** {config.COIN_NAME}(s) but only have **{u['coins']}**.",
            ephemeral=True,
        )
        return

    bot._buying.add(key)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        # Pre-checks (no charge if they fail to pass)
        try:
            _, stock = await bot.get_store()
            count = extract_stock(stock, account_type)
            if count is not None and count <= 0:
                await _safe_followup(
                    interaction,
                    content="That account is **out of stock** right now. You were not charged.",
                    ephemeral=True,
                )
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("stock precheck failed: %s", exc)

        # Reserve coins; refund on any failure below.
        await bot.db.add_coins(guild_id, uid, -cost)
        try:
            _, data = await bot.nfa_post(
                "/api/v1/create_keys",
                {"account_type": account_type, "amount": 1},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            await bot.db.add_coins(guild_id, uid, cost)
            log.warning("create_keys error: %s", exc)
            await _safe_followup(
                interaction,
                content="The store errored while generating your key. "
                "You were **refunded** \u2014 please try again shortly.",
                ephemeral=True,
            )
            return

        keys = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("status") != "success" or not keys:
            await bot.db.add_coins(guild_id, uid, cost)
            msg = data.get("message") if isinstance(data, dict) else "Unknown error"
            await _safe_followup(
                interaction,
                content=f"Purchase failed: {msg}\nYou were **refunded**.",
                ephemeral=True,
            )
            return

        await _deliver_key(interaction, tier, keys[0])
    finally:
        bot._buying.discard(key)


@bot.tree.command(
    name="replace",
    description="Replace an invalid account key (within the 3-hour warranty)",
)
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 120.0, key=lambda i: i.user.id)
@app_commands.describe(key="Your activation key to check / replace")
async def replace(interaction: discord.Interaction, key: str):
    key = key.strip()
    if not key:
        await interaction.response.send_message(
            "Please provide your activation key.", ephemeral=True
        )
        return
    if not config.NFA_API_KEY:
        await interaction.response.send_message(
            "Replacements aren't configured yet (an admin must set `NFA_API_KEY`).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        _, data = await bot.nfa_post(
            "/api/v1/check_account", {"activation_key": key}, timeout=30
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("check_account error: %s", exc)
        await _safe_followup(
            interaction,
            content="Couldn't reach the replacement service \u2014 please try again shortly.",
            ephemeral=True,
        )
        return

    result = data.get("result") if isinstance(data, dict) else None
    message = data.get("message") if isinstance(data, dict) else None

    if result == "valid":
        await _safe_followup(
            interaction,
            content="\u2705 That account is still **valid** \u2014 no replacement needed.",
            ephemeral=True,
        )
    elif result == "replaced":
        replacement = data.get("replacement_key") if isinstance(data, dict) else None
        body = "\U0001F504 Your account was **replaced** under the 3-hour warranty.\n\n"
        if replacement:
            body += (
                f"**New key:** ||`{replacement}`||\n\n"
                f"Activate it at {ACTIVATION_URL} \u2014 keep it private, treat it like cash."
            )
        else:
            body += "Check the panel for your new replacement key."
        await _safe_followup(interaction, content=body, ephemeral=True)
    else:
        note = message or (
            "the account is still valid or outside the 3-hour warranty window"
        )
        await _safe_followup(
            interaction,
            content=f"\u274C No replacement issued \u2014 {note}. "
            "If you think this is a mistake, contact support.",
            ephemeral=True,
        )


# --------------------------- admin commands ---------------------------
@app_commands.guild_only()
class AdminGroup(app_commands.Group):
    def __init__(self, bot_: StatusBot):
        super().__init__(name="admin", description="Status-coin bot administration")
        self.bot = bot_

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in config.ADMIN_USER_IDS:
            return True
        msg = "\u26D4 You're not authorised to use the admin commands."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    @app_commands.command(
        name="liveleaderboard",
        description="Post a self-updating leaderboard in a channel",
    )
    @app_commands.describe(channel="Channel for the leaderboard (defaults to here)")
    async def liveleaderboard_cmd(
        self, interaction: discord.Interaction, channel: discord.TextChannel = None
    ):
        channel = channel or interaction.channel
        existing = await self.bot.db.get_live_leaderboard(interaction.guild_id)
        if existing:
            old = interaction.guild.get_channel(existing["channel_id"])
            if old is not None:
                try:
                    await old.get_partial_message(existing["message_id"]).delete()
                except discord.HTTPException:
                    pass
        embed = await build_leaderboard_embed(interaction.guild, live=True)
        try:
            message = await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to post in {channel.mention}.", ephemeral=True
            )
            return
        await self.bot.db.set_live_leaderboard(
            interaction.guild_id, channel.id, message.id
        )
        await interaction.response.send_message(
            f"\u2705 Live leaderboard posted in {channel.mention} \u2014 it refreshes every minute.",
            ephemeral=True,
        )

    @app_commands.command(
        name="stopleaderboard", description="Stop and remove the live leaderboard"
    )
    async def stopleaderboard_cmd(self, interaction: discord.Interaction):
        existing = await self.bot.db.get_live_leaderboard(interaction.guild_id)
        if not existing:
            await interaction.response.send_message(
                "There's no live leaderboard running.", ephemeral=True
            )
            return
        chan = interaction.guild.get_channel(existing["channel_id"])
        if chan is not None:
            try:
                await chan.get_partial_message(existing["message_id"]).delete()
            except discord.HTTPException:
                pass
        await self.bot.db.clear_live_leaderboard(interaction.guild_id)
        await interaction.response.send_message(
            "\u2705 Live leaderboard stopped and removed.", ephemeral=True
        )

    @app_commands.command(name="accounts", description="List real NFA account types, prices and stock")
    async def accounts_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not config.NFA_API_KEY:
            await interaction.followup.send("NFA_API_KEY is not set.", ephemeral=True)
            return
        try:
            accounts, stock = await self.bot.get_store()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"API error: `{exc}`", ephemeral=True)
            return
        if not accounts:
            await interaction.followup.send(
                "No account types returned (check the API key).", ephemeral=True
            )
            return
        lines = []
        for name, price in sorted(accounts.items()):
            count = extract_stock(stock, name)
            count_str = count if count is not None else "n/a"
            lines.append(f"`{name}` \u2014 ${price} \u00b7 {count_str} stock")
        embed = discord.Embed(
            title="NFA Account Types (live)",
            description="\n".join(lines)[:4000],
            color=config.EMBED_COLOR,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="addcoins", description="Add (or subtract) a user's coins")
    @app_commands.describe(user="Member", amount="Amount (use a negative number to remove)")
    async def addcoins(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        new = await self.bot.db.add_coins(interaction.guild_id, user.id, amount)
        await interaction.response.send_message(
            f"{user.mention} now has **{new}** {config.COIN_NAME}(s).", ephemeral=True
        )

    @app_commands.command(name="removecoins", description="Remove coins from a user")
    @app_commands.describe(user="Member", amount="How many to remove")
    async def removecoins(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        new = await self.bot.db.add_coins(interaction.guild_id, user.id, -abs(amount))
        await interaction.response.send_message(
            f"{user.mention} now has **{new}** {config.COIN_NAME}(s).", ephemeral=True
        )

    @app_commands.command(name="setcoins", description="Set a user's coin balance")
    async def setcoins(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        new = await self.bot.db.set_coins(interaction.guild_id, user.id, amount)
        await interaction.response.send_message(
            f"Set {user.mention} to **{new}** {config.COIN_NAME}(s).", ephemeral=True
        )

    @app_commands.command(name="setrequiredstatus", description="Set the required custom-status text")
    async def setrequiredstatus(self, interaction: discord.Interaction, text: str):
        await self.bot.db.update_setting(interaction.guild_id, "required_status", text)
        self.bot.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Required status updated to:\n> `{text}`", ephemeral=True
        )

    @app_commands.command(name="setrewardhours", description="Hours of online time per coin reward")
    async def setrewardhours(self, interaction: discord.Interaction, hours: float):
        await self.bot.db.update_setting(interaction.guild_id, "reward_seconds", hours * 3600)
        self.bot.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Reward interval set to **{hours:g} hours**.", ephemeral=True
        )

    @app_commands.command(name="setcoinsperreward", description="Coins granted each interval")
    async def setcoinsperreward(self, interaction: discord.Interaction, amount: int):
        amount = max(1, amount)
        await self.bot.db.update_setting(interaction.guild_id, "coins_per_reward", amount)
        self.bot.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Coins per reward set to **{amount}**.", ephemeral=True
        )

    @app_commands.command(name="seteligiblestatuses", description="e.g. 'online,dnd'")
    async def seteligiblestatuses(self, interaction: discord.Interaction, statuses: str):
        cleaned = ",".join(
            p.strip().lower()
            for p in statuses.split(",")
            if p.strip().lower() in _STATUS_MAP
        ) or "online,dnd"
        await self.bot.db.update_setting(interaction.guild_id, "eligible_statuses", cleaned)
        self.bot.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Eligible statuses set to: `{cleaned}`", ephemeral=True
        )

    @app_commands.command(name="setlogchannel", description="Channel for reward announcements")
    async def setlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.update_setting(interaction.guild_id, "log_channel_id", channel.id)
        self.bot.invalidate_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"Reward log channel set to {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="reset", description="Reset a user's coins and time")
    async def reset(self, interaction: discord.Interaction, user: discord.Member):
        await self.bot.db.upsert_user(
            interaction.guild_id, user.id,
            total_eligible_seconds=0, coins=0, rewards_count=0,
        )
        self.bot.eligible_since.pop((interaction.guild_id, user.id), None)
        await interaction.response.send_message(f"Reset {user.mention}.", ephemeral=True)

    @app_commands.command(name="settings", description="View current bot settings")
    async def settings_cmd(self, interaction: discord.Interaction):
        s = await self.bot.get_guild_settings(interaction.guild_id)
        live = sum(1 for k in self.bot.eligible_since if k[0] == interaction.guild_id)
        ch = s.get("log_channel_id")
        embed = discord.Embed(title="Current Settings", color=config.EMBED_COLOR)
        embed.add_field(name="Required status", value=f"`{s['required_status']}`", inline=False)
        embed.add_field(name="Reward interval", value=f"{s['reward_seconds'] / 3600:g} h", inline=True)
        embed.add_field(name="Coins per reward", value=str(s["coins_per_reward"]), inline=True)
        embed.add_field(name="Eligible statuses", value=s["eligible_statuses"], inline=True)
        embed.add_field(name="Log channel", value=f"<#{ch}>" if ch else "(none)", inline=True)
        embed.add_field(name="Earning right now", value=str(live), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def main():
    if not config.TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and paste your bot token."
        )
    try:
        bot.run(config.TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Privileged intents are required. In the Discord Developer Portal > your app "
            "> Bot, enable BOTH 'Server Members Intent' and 'Presence Intent', then retry."
        )
    except discord.LoginFailure:
        raise SystemExit("Login failed: your DISCORD_TOKEN is invalid.")


if __name__ == "__main__":
    main()
