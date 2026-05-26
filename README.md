# telegram2qbittorrent

A small Telegram bot that adds torrents to a qBittorrent instance on your LAN — so you can queue downloads from anywhere without exposing the qBittorrent Web UI to the internet.

## How it works

```
Telegram (you) ──▶ bot (your home server) ──▶ qBittorrent Web UI
```

The bot listens for messages from authorized Telegram accounts. Two inputs are supported:

- A `.torrent` file attachment.
- A `magnet:` link as text.

Anything else is ignored or rejected. Only Telegram user IDs you list in `.env` can issue commands; all other users get `Unauthorized.`

## Prerequisites

- Ubuntu (or any Debian-based) host on your LAN, with `systemd`.
- qBittorrent running on the same LAN with the Web UI enabled (Tools → Options → Web UI), reachable from the host that will run the bot.
- A Telegram bot token — talk to [@BotFather](https://t.me/BotFather), use `/newbot`.
- Your Telegram user ID — talk to [@userinfobot](https://t.me/userinfobot) and copy the numeric `Id`.

## Installation

On the server:

```bash
sudo apt install -y git
git clone https://github.com/evl-alex/telegram2qbittorrent.git ~/telegram2qbittorrent
cd ~/telegram2qbittorrent

# First run: installs dependencies and scaffolds .env + save_paths.json
./deploy/install.sh

# Fill in real values
nano .env
nano save_paths.json

# Second run: installs the systemd unit and starts the service
./deploy/install.sh
```

[`deploy/install.sh`](deploy/install.sh) is idempotent — safe to re-run. It detects your username and install path automatically, so it works whether the repo lives in `/home/user/…`, `/home/pi/…`, `/srv/…`, etc.

### Configuration

[`.env.example`](.env.example) is the template. Copy to `.env` and fill in:

| Variable | Meaning |
| --- | --- |
| `BOT_TOKEN` | Token from @BotFather. |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot. At least one required. |
| `QB_HOST` | Host where qBittorrent Web UI is reachable (e.g. `127.0.0.1` if same machine, or LAN IP). |
| `QB_PORT` | qBittorrent Web UI port (default `8080`). |
| `QB_USER` / `QB_PASS` | qBittorrent Web UI credentials. |

`.env` is gitignored — secrets stay on the server.

Save destinations live in [`save_paths.json`](save_paths.example.json) — a JSON
array of `{"label", "path"}` entries, in the order the buttons should appear.
The file is gitignored, so edits survive `deploy/update.sh`. At least one entry
is required; with exactly one entry the bot skips the picker and adds every
torrent straight to that path.

```json
[
  {"label": "TV Series",  "path": "/mnt/media/tv"},
  {"label": "Movies",     "path": "/mnt/media/movies"},
  {"label": "Downloads",  "path": "/mnt/media/downloads"}
]
```

Adding a destination: append an entry and restart the service
(`sudo systemctl restart telegram2qbittorrent`).

## Operation

### Sending a torrent

From an authorized Telegram account, DM the bot:

- Attach a `.torrent` file or paste a `magnet:` link.
- If `save_paths.json` has more than one entry, the bot replies
  `Where should this torrent be saved?` with one inline button per entry.
  Tap one → bot edits the message to `✅ Added to qBittorrent → <label>`.
- If `save_paths.json` has exactly one entry, the picker is skipped and the
  bot replies immediately with `✅ Added to qBittorrent → <label>`.
- The same message then refreshes every ~10 seconds with live progress
  (`📥 Downloading <name> → <label>` plus percent, speed, seeds, ETA),
  and flips to `✅ <name> successfully downloaded to <label> in <elapsed>`
  when the torrent finishes. Live updates are dropped if the bot restarts
  mid-download; the torrent itself continues in qBittorrent.
- On failure → bot replies `❌ Error: <details>`.

Pending uploads are held in memory until the user picks a destination. If the
bot restarts before a button is tapped, the picker message will reply
`⚠️ This upload expired, please re-send.`

### Viewing logs

```bash
journalctl -u telegram2qbittorrent -f          # follow live
journalctl -u telegram2qbittorrent -n 100      # last 100 lines
journalctl -u telegram2qbittorrent --since today
```

### Service control

```bash
sudo systemctl status telegram2qbittorrent
sudo systemctl restart telegram2qbittorrent
sudo systemctl stop telegram2qbittorrent
sudo systemctl start telegram2qbittorrent
```

The service is enabled at install time, so it auto-starts on boot and auto-restarts on crash (5-second delay).

### Updating to the latest version

If project's GitHub repo has changes that you want to apply, run following script:

```bash
~/telegram2qbittorrent/deploy/update.sh
```

[`deploy/update.sh`](deploy/update.sh) fetches, fast-forwards, reinstalls dependencies only if [`requirements.txt`](requirements.txt) changed, and restarts the service. If there's nothing new, it exits without touching the service.

## Development

Standard git workflow on each dev machine:

```bash
git clone https://github.com/evl-alex/telegram2qbittorrent.git
cd telegram2qbittorrent
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # only if you want to run the bot locally for testing
```

Edit, commit, push. Then on the server: `deploy/update.sh`.

`.env` is per-machine. Most dev machines won't need one — code edits don't require running the bot locally.

## Project layout

- [`main.py`](main.py) — the bot itself (~250 lines).
- [`requirements.txt`](requirements.txt) — Python dependencies.
- [`.env.example`](.env.example) — secrets/connection config template.
- [`save_paths.example.json`](save_paths.example.json) — save-destination template.
- [`deploy/install.sh`](deploy/install.sh) — first-time server setup.
- [`deploy/update.sh`](deploy/update.sh) — pull + restart.
- [`deploy/telegram2qbittorrent.service`](deploy/telegram2qbittorrent.service) — systemd unit template (placeholders substituted by `install.sh`).

## Troubleshooting

**Service won't start, logs show "qBittorrent login failed: invalid credentials"**
Check `QB_USER` / `QB_PASS` in `.env`. The bot logs into qBittorrent at startup; bad credentials are fatal.

**Service won't start, logs show "Could not connect to qBittorrent"**
qBittorrent isn't reachable at `QB_HOST:QB_PORT`. Verify the Web UI is enabled and the port is open on the LAN. From the server: `curl http://$QB_HOST:$QB_PORT`.

**Bot replies `Unauthorized.`**
Your Telegram user ID isn't in `ALLOWED_USER_IDS`. Confirm the numeric ID via [@userinfobot](https://t.me/userinfobot), add it (comma-separated for multiple users), and restart: `sudo systemctl restart telegram2qbittorrent`.

**Bot replies `❌ Error: qBittorrent rejected the torrent`**
The file or magnet link is malformed, or qBittorrent already has it. Check the qBittorrent Web UI — the torrent may already be queued.

**Bot doesn't respond at all**
Check `systemctl status telegram2qbittorrent`. If active, check `journalctl -u telegram2qbittorrent -n 50`. If inactive, the service crashed and couldn't restart — fix whatever the logs report, then `sudo systemctl start telegram2qbittorrent`.
