# Status-Coin Bot

A Discord bot that rewards members for advertising while they're online.

Members earn coins for **accumulated time spent online (or DND) while showing a
required custom status** (default: `$1.20 Rust: nfaccount.com`). By default they
get **1 coin per 48 hours** of that eligible online time. Time only counts while
they are online **and** displaying the status.

## Features

- Live presence tracking (online / dnd) + custom-status text matching
- Coin reward engine: configurable interval (default 48h) and coins per reward
- Only online time with the correct status counts toward the timer
- The bot itself displays the advertising status
- Reward announcements in a log channel
- Slash commands:
  - `/balance [user]` - coins + progress to next coin
  - `/check [user]` - shows exactly why you do/don't qualify
  - `/leaderboard` - top coin holders
  - `/pay <user> <amount>` - transfer coins
  - `/howitworks` - explains the system
- Admin commands (require **Manage Server**), under `/admin`:
  - `addcoins`, `removecoins`, `setcoins`, `reset`
  - `setrequiredstatus`, `setrewardhours`, `setcoinsperreward`
  - `seteligiblestatuses`, `setlogchannel`, `settings`

## Setup (Windows, beginner friendly)

### 1. Install Python
Install Python 3.10+ from https://python.org (check "Add Python to PATH").

### 2. Create the bot application
1. Go to https://discord.com/developers/applications -> **New Application**.
2. Open **Bot** -> **Reset Token** -> copy the token.
3. On the same Bot page, enable **BOTH**:
   - **Server Members Intent**
   - **Presence Intent**
   (Required - the bot cannot see statuses without these.)

### 3. Invite the bot
On **OAuth2 > URL Generator**, tick:
- Scopes: `bot`, `applications.commands`
- Bot Permissions: `Send Messages`, `Embed Links`

Open the generated URL and add the bot to your server.

### 4. Configure
1. Copy `.env.example` to `.env`.
2. Paste your token into `DISCORD_TOKEN`.
3. (Recommended) Put your server ID in `GUILD_ID` for instant slash commands.

### 5. Run
Double-click **`start.bat`** (it creates a venv, installs deps, and runs the bot).

Or manually:
```bat
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## How earning works

1. A member sets their Discord **custom status** to include the required text.
2. While they are **online/dnd** with that status, their eligible time counts up.
3. Every **48h** of eligible time -> **1 coin** (both configurable).

If they go offline/idle or remove the status, the timer pauses; it resumes when
they qualify again. Bot downtime does **not** count.

## Notes / assumptions

- "Every 48h, only when online" is implemented as **48h of accumulated online
  time with the status**, not 48h of wall-clock time. This is the fairest reading
  and is fully configurable with `/admin setrewardhours`.
- Custom-status matching is **case-insensitive substring** (their status must
  *contain* the required text).
- Data is stored locally in `bot_data.db` (SQLite).

## Changing the required status later
Use `/admin setrequiredstatus <text>` in Discord, or edit `REQUIRED_STATUS` in
`.env` and restart.
