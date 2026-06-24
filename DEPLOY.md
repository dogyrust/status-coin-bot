# Hosting the bot 24/7

Honest truth up front: **"free + 24/7 + super easy" can't all be true at once** with
mainstream hosts right now.

| Option | 24/7 | Free | Easy |
|--------|------|------|------|
| **Railway** | Yes | No (~$5/mo after a one-time trial credit) | Very easy |
| **Oracle Cloud (Always Free)** | Yes | Yes, truly | Medium (~15 min once) |
| **Your PC** | Only while PC is on | Yes | Very easy |

Both setups below use the included `Dockerfile`, so you don't manage Python on the server.

---

## Option A — Railway (easiest; ~$5/mo after trial)

1. Put this project on **GitHub** (new repo, push the folder).
2. Go to https://railway.app -> **New Project** -> **Deploy from GitHub repo** -> pick it.
3. Railway sees the `Dockerfile` and builds automatically.
4. Open the service -> **Variables** -> add:
   - `DISCORD_TOKEN` = your token
   - `GUILD_ID` = your server ID
   - `DB_PATH` = `/data/bot_data.db`
5. **Keep balances after redeploys:** service -> **Volumes** -> add a volume mounted at `/data`.
6. It now runs 24/7. Check **Deploy Logs** for `Logged in as ...`.

> Paste the token only into Railway's **Variables** screen — never into the code or chat.

---

## Option B — Oracle Cloud "Always Free" (free + 24/7)

### 1. Make a free server
- Sign up at https://www.oracle.com/cloud/free/ (needs a card for ID check; Always Free
  resources are not charged).
- **Create a VM instance** -> Image **Ubuntu 22.04** -> Shape: an **Always Free** one
  (e.g. `VM.Standard.A1.Flex`, 1 OCPU / 6 GB, or `VM.Standard.E2.1.Micro`).
- Save the SSH key it gives you. Note the public IP.

### 2. Connect
```bash
ssh ubuntu@YOUR_SERVER_IP
```

### 3. Install Docker + get the code
```bash
sudo apt update && sudo apt install -y docker.io git
sudo systemctl enable --now docker
git clone YOUR_GITHUB_REPO_URL status-coin-bot
cd status-coin-bot
```

### 4. Put your secrets in a private file (not in chat/code)
```bash
sudo tee /opt/status-coin-bot.env >/dev/null <<'EOF'
DISCORD_TOKEN=PASTE_YOUR_TOKEN_HERE
GUILD_ID=PASTE_YOUR_SERVER_ID_HERE
DB_PATH=/data/bot_data.db
EOF
sudo chmod 600 /opt/status-coin-bot.env
```

### 5. Build and run (auto-restarts, survives reboots)
```bash
sudo docker build -t status-coin-bot .
sudo mkdir -p /opt/botdata
sudo docker run -d --name status-coin-bot --restart always \
  --env-file /opt/status-coin-bot.env \
  -v /opt/botdata:/data \
  status-coin-bot
```

### 6. Check it / manage it
```bash
sudo docker logs -f status-coin-bot     # watch logs (expect "Logged in as ...")
sudo docker restart status-coin-bot     # restart
```

**Updating later:** `git pull` then re-run the build + a fresh `docker run`
(`sudo docker rm -f status-coin-bot` first).

---

## Persistence note
The bot stores balances in `bot_data.db`. The `-v .../data` volume (Oracle) and the
`/data` volume (Railway) keep that file safe across restarts/redeploys, with
`DB_PATH=/data/bot_data.db`. Without a volume, balances reset on each redeploy.
