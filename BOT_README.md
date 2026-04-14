# AWP Mine Telegram Bot

Telegram bot to remotely control and monitor the [data4agent/mine](https://github.com/data4agent/mine) mining agent.

## Features

| Command | Description |
|---------|-------------|
| `/start` | Show menu with inline buttons |
| `/status` | Check agent status |
| `/doctor` | Run environment diagnostics |
| `/run` | Start mining (with optional dataset selection) |
| `/stop` | Stop mining |
| `/pause` | Pause mining |
| `/resume` | Resume mining |
| `/validator` | Start validator |
| `/datasets` | List available datasets |
| `/logs` | Show recent logs |
| `/diagnose` | Full connectivity diagnosis |

## Setup on VPS

### 1. Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/awp-miner-bot.git
cd awp-miner-bot

# Run mine bootstrap (sets up Python venv + awp-wallet)
bash scripts/bootstrap.sh

# Install bot dependencies
pip install -r requirements-bot.txt
```

### 2. Configure

```bash
cp .env.example .env
nano .env
```

Set your `TELEGRAM_BOT_TOKEN` and optionally `ALLOWED_USER_IDS`.

### 3. Run

```bash
# Direct
python bot.py

# Or with systemd (recommended for VPS)
sudo cp awp-mine-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable awp-mine-bot
sudo systemctl start awp-mine-bot
```

### 4. Check Logs

```bash
sudo journalctl -u awp-mine-bot -f
```

## Systemd Service

A `awp-mine-bot.service` file is included for easy VPS deployment.

## Security

- Set `ALLOWED_USER_IDS` in `.env` to restrict bot access to your Telegram account only.
- Never commit `.env` to Git (it's in `.gitignore`).
