"""Status-Coin Bot.

Tracks members' Discord presence. A member earns coins for accumulated time
spent ONLINE (or DND) while displaying a required custom-status text.
By default: 1 coin per 48h of eligible online time.

Only time while online/dnd AND showing the status counts toward the 48h.
"""
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from db import Database

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


# --------------------------- bot ---------------------------
class StatusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(config.DB_PATH)
        # (guild_id, user_id) -> unix timestamp when they became eligible.
        self.eligible_since = {}
        self._settings_cache = {}

    async def setup_hook(self):
        await self.db.connect()
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
        await self.db.close()
        await super().close()


bot = StatusBot()


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


@bot.tree.command(name="leaderboard", description="Top coin holders in this server")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    rows = await bot.db.leaderboard(interaction.guild_id, 10)
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = []
    for i, row in enumerate(rows):
        member = interaction.guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        prefix = medals[i] if i < 3 else f"`#{i + 1}`"
        lines.append(f"{prefix} **{name}** — {row['coins']} {config.COIN_NAME}(s)")
    embed = discord.Embed(
        title=f"{config.COIN_EMOJI} Leaderboard",
        description="\n".join(lines) if lines else "No one has earned coins yet.",
        color=config.EMBED_COLOR,
    )
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


# --------------------------- admin commands ---------------------------
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class AdminGroup(app_commands.Group):
    def __init__(self, bot_: StatusBot):
        super().__init__(name="admin", description="Status-coin bot administration")
        self.bot = bot_

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
