"""
AWP Mine Telegram Bot — Clean Build
Controls awp-miner agents via Telegram commands.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
SCRIPTS    = BASE_DIR / "scripts"
RUN_TOOL   = SCRIPTS / "run_tool.py"
OUTPUT_DIR = BASE_DIR / "output" / "agent-runs"

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    level=logging.INFO)
log = logging.getLogger("awp-bot")

# ── Env ────────────────────────────────────────────────────────────────────
load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_ids   = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED    = {int(x) for x in _raw_ids.split(",") if x.strip().isdigit()}


# ══════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _clean_env() -> dict:
    """
    Return a 'Clean Room' environment for spawning awp-wallet / run_tool.
    PM2 / PTY variables (NODE_APP_INSTANCE, PM2_HOME …) crash Node.js ≥ 22
    (SIGABRT / exit -6) when passed to child processes.
    We pass ONLY what the child actually needs.
    """
    local_bin = str(Path.home() / ".local" / "bin")
    base_path = f"{local_bin}:/usr/local/bin:/usr/bin:/bin"

    env: dict[str, str] = {
        "PATH":            base_path,
        "HOME":            str(Path.home()),
        "LANG":            "en_US.UTF-8",
        "LC_ALL":          "en_US.UTF-8",
        "NODE_NO_WARNINGS":"1",
        "USER":            os.environ.get("USER", "ubuntu"),
        # Force Python to flush stdout/stderr immediately
        "PYTHONUNBUFFERED":"1",
        # Tell run_tool not to re-exec itself into venv (we already choose the binary)
        "MINE_SKIP_VENV_REEXEC": "1",
    }

    # Forward all AWP_* and MINE_* vars from our .env
    for key, val in os.environ.items():
        if key.startswith(("AWP_", "MINE_", "PLATFORM_", "MINER_")):
            env[key] = val

    return env


def _py() -> str:
    """Resolve the correct Python binary (prefer local venv)."""
    venv_py = BASE_DIR / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def _auth(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if ALLOWED and uid not in ALLOWED:
        return False
    return True


def _esc(text: str) -> str:
    """Escape reserved MarkdownV2 characters for normal text."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _running_miner_pid() -> int | None:
    """Return PID of a running run_tool.py process, or None."""
    try:
        r = subprocess.run(["pgrep", "-f", "run_tool.py"],
                           capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.strip().splitlines() if p.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _kill_miner():
    """Kill any running miner process."""
    subprocess.run(["pkill", "-f", "run_tool.py"], capture_output=True)
    subprocess.run(["pm2", "delete", "awp-miner-v2"], capture_output=True)
    subprocess.run(["pm2", "stop", "awp-benchmark"], capture_output=True)


# ══════════════════════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    text = (
        "⛏️ *AWP Mine Bot*\n\n"
        "Commands:\n"
        "• `/switch 2` — Start Mining on Worknet 2 \\(stake\\-free\\)\n"
        "• `/miner` — Show live mining log\n"
        "• `/stop` — Stop all workers\n"
        "• `/status` — Show miner status\n"
        "• `/wallet` — Show wallet address\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch worknet. Stake-free worknets (2+) launch miner directly."""
    if not _auth(update):
        return

    if not context.args:
        await update.message.reply_text(
            "❓ Usage: `/switch <id>`\nExample: `/switch 2`")
        return

    wn_id = context.args[0].strip()
    msg = await update.message.reply_text(f"⏳ Switching to Worknet {wn_id}…")

    try:
        # ── Worknet 2+ : stake-free, no contract allocation needed ──────────
        if wn_id != "1":
            _kill_miner()

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            log_file = OUTPUT_DIR / f"mine-{int(time.time())}.log"

            env = _clean_env()

            with open(str(log_file), "w") as lf:
                subprocess.Popen(
                    [_py(), str(RUN_TOOL), "run-loop", "60", "0"],
                    stdout=lf,
                    stderr=lf,
                    cwd=str(BASE_DIR),
                    env=env,
                    start_new_session=True,   # detach from bot process group
                )

            await msg.edit_text(
                f"✅ *Worknet {wn_id} Mining ACTIVE\\!*\n"
                f"📄 Log: `{log_file.name}`\n"
                f"Use /miner to monitor\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # ── Worknet 1 : Benchmark (needs allocation) ─────────────────────
        await msg.edit_text("⚠️ Worknet 1 (Benchmark) requires stake — not implemented in this build.")

    except Exception as e:
        log.exception("cmd_switch error")
        await msg.edit_text(f"❌ Error: {str(e)}")


async def cmd_miner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the tail of the latest mining log."""
    if not _auth(update):
        return

    try:
        if not OUTPUT_DIR.exists():
            await update.message.reply_text("💤 No mining output directory found yet.")
            return

        logs = sorted(OUTPUT_DIR.glob("mine-*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            await update.message.reply_text(
                "💤 No mining logs found.\nRun /switch 2 to start the miner.")
            return

        latest = logs[-1]
        result = subprocess.run(
            ["tail", "-n", "30", str(latest)],
            capture_output=True, text=True, timeout=5
        )
        content = result.stdout.strip()

        pid = _running_miner_pid()
        status_icon = "🟢" if pid else "🔴"
        header = f"{status_icon} Mining Activity — `{latest.name}`"

        if not content:
            await update.message.reply_text(
                f"{header}\n\n_Log is empty — miner may still be starting up\\. Wait 60s and try again\\._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Inside a pre/code block, only ` and \ need escaping
        safe = content[:3500].replace("\\", "\\\\").replace("`", "\\`")
        await update.message.reply_text(
            f"*{header}*\n\n```\n{safe}\n```",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        log.exception("cmd_miner error")
        await update.message.reply_text(f"❌ Error reading logs: {str(e)}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop all running miners."""
    if not _auth(update):
        return
    msg = await update.message.reply_text("🛑 Stopping all miners…")
    _kill_miner()
    await msg.edit_text("✅ All miners stopped.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show running miner PID if any."""
    if not _auth(update):
        return
    pid = _running_miner_pid()
    if pid:
        await update.message.reply_text(f"🟢 Miner is running \\(PID {pid}\\)\\.\nUse /miner to see logs\\.",
                                        parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("🔴 No miner is running\\.\nUse /switch 2 to start\\.",
                                        parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the wallet address."""
    if not _auth(update):
        return
    msg = await update.message.reply_text("🔍 Fetching wallet address…")
    try:
        wallet_bin = os.getenv("AWP_WALLET_BIN",
                               str(Path.home() / ".local" / "bin" / "awp-wallet"))
        r = subprocess.run(
            [wallet_bin, "receive", "--chain", "base"],
            capture_output=True, text=True, timeout=30,
            env=_clean_env(),
        )
        data = json.loads(r.stdout or "{}")
        addr = data.get("eoaAddress") or data.get("address") or "Not found"
        await msg.edit_text(f"💳 Wallet: `{_esc(addr)}`",
                            parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

async def _post_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    await app.bot.set_my_commands([
        BotCommand("start",  "Show help"),
        BotCommand("switch", "Switch worknet (e.g. /switch 2)"),
        BotCommand("miner",  "Show live mining log"),
        BotCommand("stop",   "Stop all miners"),
        BotCommand("status", "Show miner status"),
        BotCommand("wallet", "Show wallet address"),
    ])
    log.info("Bot ready.")


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("miner",  cmd_miner))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("wallet", cmd_wallet))

    log.info("Polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
