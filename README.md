# discord-device-status

Discord bot that reports **battery** (if present) and **CPU / RAM / load / uptime** for the Linux machine it runs on.

The same code works on this laptop and on a Raspberry Pi. A Pi with no battery still answers with usage; a Pi UPS HAT that shows up under `/sys/class/power_supply` is included automatically.

Discord only allows **one gateway connection per bot token**. To watch two machines at once, create two Discord applications (two tokens) and run one process on each host.

## Setup (laptop or Raspberry Pi)

Needs Python 3.10+ (Pi OS Bookworm and CachyOS both qualify).

```bash
cd ~/Projects/discord-device-status   # or wherever you cloned
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. Create an application at https://discord.com/developers/applications
2. Bot → Reset Token → paste into `DISCORD_TOKEN` in `.env`
3. Bot → Privileged Gateway Intents → enable **Message Content Intent**
4. OAuth2 → URL Generator → scopes `bot` and `applications.commands` → invite to a server (required before DMs work)
5. Enable Developer Mode in Discord, copy your user ID, set `ALLOWED_USER_IDS`
6. Optional: `DEVICE_NAME=pi` or `DEVICE_NAME=laptop` so replies are easy to tell apart

```bash
python status.py    # local test, no Discord needed
python bot.py
```

Ask with `/status` in a server, or DM `status`.

## Canned replies

Edit [`replies.csv`](replies.csv) — two columns, `prompt` and `response`. Matching is exact and case-insensitive. The bot reloads the file on the next message after you save, so you do not need to restart.

```csv
prompt,response
hello bot,hello human
how are you,"i am a laptop, thanks"
```

If a response contains a comma, wrap it in double quotes. `status` is reserved for the device report and is not taken from this table. Canned replies are open to anyone who can message the bot; device status stays allowlisted.

## systemd (optional)

Adjust `WorkingDirectory` / `ExecStart` if the repo is not in `~/Projects/discord-device-status`.

```bash
mkdir -p ~/.config/systemd/user
cp discord-device-status.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-device-status.service
loginctl enable-linger "$USER"   # keep it running after logout
```
