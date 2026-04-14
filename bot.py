"""
AWP Mine Telegram Bot
Controls the data4agent/mine skill via Telegram commands.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts"
RUN_TOOL = SCRIPTS_DIR / "run_tool.py"
OUTPUT_DIR = BASE_DIR / "output" / "agent-runs"
WORKER_STATE_DIR = OUTPUT_DIR / "_worker_state"
AWP_WALLET_BIN = os.getenv("AWP_WALLET_BIN", str(Path.home() / ".local" / "bin" / "awp-wallet"))

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("awp-bot")

# ── Load env ───────────────────────────────────────────────────────
load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = set()
if ALLOWED_USER_IDS_RAW.strip():
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in ALLOWED_USER_IDS_RAW.split(",") if uid.strip()}

# ── In-memory log buffer ──────────────────────────────────────────
LOG_BUFFER: deque[str] = deque(maxlen=100)


# ── Helpers ────────────────────────────────────────────────────────

def _python_bin() -> str:
    """Resolve the Python binary — prefer local venv."""
    # Check if we are running in a venv already
    if sys.prefix != sys.base_prefix:
        return sys.executable

    venv_python = BASE_DIR / ".venv" / ("bin" if os.name != "nt" else "Scripts") / ("python" if os.name != "nt" else "python.exe")
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _run_tool(command: str, *args: str, timeout: int = 120) -> dict:
    """Execute ``python scripts/run_tool.py <command> [args]`` and return parsed JSON or raw text."""
    cmd = [_python_bin(), str(RUN_TOOL), command, *args]
    # ──────────────────────────────────────────────────────────────────────────
    # CRITICAL FIX: ISOLATED ENVIRONMENT
    # Node.js 22 sometimes crashes (exit -6) when inheriting a Python/PM2 env.
    # We create a "Clean Room" env with ONLY essential variables.
    # ──────────────────────────────────────────────────────────────────────────
    isolated_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PWD": str(BASE_DIR),
        "NODE_NO_WARNINGS": "1",
        "USER": os.environ.get("USER", "ubuntu")
    }

    # Add back the critical AWP/LLM vars from our loaded .env
    critical_keys = [
        "AWP_WALLET_TOKEN", "AWP_WALLET_BIN", "AWP_API_URL", "PLATFORM_BASE_URL",
        "MINE_GATEWAY_BASE_URL", "MINE_GATEWAY_TOKEN", "MINE_GATEWAY_MODEL",
        "MINE_ENRICH_MODE", "MINE_ENRICH_MODEL", "MINE_LLM_MODE", "MINER_ID"
    ]
    for key in critical_keys:
        val = os.getenv(key)
        if val:
            isolated_env[key] = val

    # Ensure our PATH includes the wallet bin if it's in a non-standard place
    wallet_bin_dir = str(Path.home() / ".local" / "bin")
    if os.path.exists(wallet_bin_dir):
        isolated_env["PATH"] = f"{wallet_bin_dir}:{isolated_env['PATH']}"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
            env=isolated_env, # Use the clean environment
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode != 0:
            logger.error(f"Command '{command}' failed with exit code {proc.returncode}")
            if stderr:
                logger.error(f"Error output: {stderr}")

        # Log for /logs command
        ts = time.strftime("%H:%M:%S")
        LOG_BUFFER.append(f"[{ts}] $ {command} (exit {proc.returncode})")
        if stdout:
            for line in stdout.splitlines()[-5:]:
                LOG_BUFFER.append(f"  {line}")
        if stderr:
            for line in stderr.splitlines()[-3:]:
                LOG_BUFFER.append(f"  ⚠ {line}")

        # Try JSON parse
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"_raw": stdout, "_stderr": stderr, "_returncode": proc.returncode}

    except subprocess.TimeoutExpired:
        return {"_raw": "", "_stderr": "Command timed out", "_returncode": -1}
    except Exception as exc:
        return {"_raw": "", "_stderr": str(exc), "_returncode": -1}


def _escape_md(text: str) -> str:
    """Escape special chars for MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!\\"
    result = []
    for ch in text:
        if ch in special:
            result.append("\\")
        result.append(ch)
    return "".join(result)


def _format_status(data: dict) -> str:
    """Format a status/response dict into a readable Telegram message."""
    if "_raw" in data:
        raw = data["_raw"] or data.get("_stderr", "No output")
        return f"```\n{raw[:3500]}\n```"

    state = data.get("state", data.get("status", "unknown"))
    user_msg = data.get("user_message", "")
    user_actions = data.get("user_actions", [])

    emoji_map = {
        "running": "🟢",
        "ready": "🟢",
        "ok": "✅",
        "idle": "⚪",
        "paused": "🟡",
        "stopped": "🔴",
        "error": "❌",
        "selection_required": "📋",
    }
    emoji = emoji_map.get(state, "ℹ️")

    lines = [f"{emoji} *State:* `{_escape_md(state)}`"]
    if user_msg:
        lines.append(f"\n{_escape_md(user_msg)}")
    if user_actions:
        lines.append(f"\n💡 *Actions:* {_escape_md(', '.join(user_actions))}")

    # Show warnings
    warnings = data.get("warnings", [])
    if warnings:
        lines.append("\n⚠️ *Warnings:*")
        for w in warnings[:5]:
            lines.append(f"  • {_escape_md(w)}")

    return "\n".join(lines)


def _auth_check(update: Update) -> bool:
    """Return True if user is allowed."""
    if not ALLOWED_USER_IDS:
        return True
    user_id = update.effective_user.id if update.effective_user else 0
    return user_id in ALLOWED_USER_IDS


async def _send(update: Update, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2):
    """Send message with fallback to plain text if markdown fails."""
    try:
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        # Fallback: send without parse mode
        plain = text.replace("\\", "")
        try:
            await update.message.reply_text(plain, reply_markup=reply_markup)
        except Exception as exc:
            await update.message.reply_text(f"Error sending message: {exc}")


# ── Command Handlers ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("🔬 Doctor", callback_data="doctor"),
        ],
        [
            InlineKeyboardButton("▶️ Start Mining", callback_data="start"),
            InlineKeyboardButton("⏹ Stop Mining", callback_data="stop"),
        ],
        [
            InlineKeyboardButton("⏸ Pause", callback_data="pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="resume"),
        ],
        [
            InlineKeyboardButton("🔍 Validator Start", callback_data="validator_start"),
            InlineKeyboardButton("📋 Logs", callback_data="logs"),
        ],
        [
            InlineKeyboardButton("🤖 LLM Config", callback_data="llm"),
        ],
    ])

    welcome = (
        "⛏ *AWP Mine Bot* ⛏\n\n"
        "Manage your AWP data mining agent from Telegram\\.\n\n"
        "*Commands:*\n"
        "• /status \\- Check agent status\n"
        "• /doctor \\- Run environment diagnostics\n"
        "• /run \\- Start mining\n"
        "• /stop \\- Stop mining\n"
        "• /pause \\- Pause mining\n"
        "• /resume \\- Resume mining\n"
        "• /validator \\- Start validator\n"
        "• /logs \\- Show recent logs\n"
        "• /datasets \\- List available datasets\n"
        "• /llm \\- Check LLM configuration\n"
    )

    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("🔄 Checking status...")
    data = _run_tool("agent-status")
    await msg.edit_text(_format_status(data), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("🔬 Running diagnostics...")
    data = _run_tool("doctor", timeout=60)

    if "_raw" in data:
        await msg.edit_text(f"```\n{data['_raw'][:3800]}\n```", parse_mode=ParseMode.MARKDOWN_V2)
        return

    status = data.get("status", "unknown")
    emoji = "✅" if status == "ok" else "❌"
    lines = [f"{emoji} *Doctor Result:* `{_escape_md(status)}`\n"]

    checks = data.get("checks", [])
    for check in checks:
        name = check.get("name", "?")
        ok = check.get("ok", False)
        icon = "✅" if ok else "❌"
        value = check.get("value", "")
        required = check.get("required", "")
        detail = f" `{_escape_md(value)}`" if value else ""
        req = f" \\(need {_escape_md(required)}\\)" if required and not ok else ""
        lines.append(f"  {icon} {_escape_md(name)}{detail}{req}")

    warnings = data.get("warnings", [])
    if warnings:
        lines.append("\n⚠️ *Warnings:*")
        for w in warnings[:5]:
            lines.append(f"  • {_escape_md(w)}")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("⛏ Starting mining agent...")
    dataset_arg = " ".join(context.args) if context.args else ""
    data = _run_tool("agent-start", dataset_arg, timeout=180) if dataset_arg else _run_tool("agent-start", timeout=180)

    state = data.get("state", "")

    # Handle dataset selection
    if state == "selection_required":
        datasets = data.get("_internal", {}).get("datasets", [])
        lines = [
            "📋 *Select a dataset to mine:*",
            "Copy and send one of the commands below:\n"
        ]
        
        for ds in datasets[:10]:
            name = str(ds.get("name") or "?")
            ds_id = str(ds.get("dataset_id") or ds.get("id") or "")
            lines.append(f"• *{_escape_md(name)}*")
            lines.append(f"  └ Command: `/run {ds_id}`")
            
        lines.append(f"\nExample: Tap the `/run ...` text to copy it\\.")
        
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        return

    await msg.edit_text(_format_status(data), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("⏹ Stopping mining...")
    data = _run_tool("agent-control", "stop", timeout=60)
    await msg.edit_text(_format_status(data), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("⏸ Pausing mining...")
    data = _run_tool("agent-control", "pause", timeout=60)
    await msg.edit_text(_format_status(data), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("▶️ Resuming mining...")
    data = _run_tool("agent-control", "resume", timeout=60)
    await msg.edit_text(_format_status(data), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_validator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("🔍 Starting validator...")
    data = _run_tool("validator-start", timeout=180)
    await msg.edit_text(_format_status(data), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_datasets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("📋 Fetching datasets...")
    data = _run_tool("list-datasets", timeout=60)

    if "_raw" in data:
        await msg.edit_text(f"```\n{data['_raw'][:3800]}\n```", parse_mode=ParseMode.MARKDOWN_V2)
        return

    datasets = data.get("datasets", data.get("items", []))
    if not datasets:
        await msg.edit_text("No datasets found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    lines = ["📋 *Available Datasets:*\n"]
    for ds in datasets[:10]:
        name = ds.get("name", ds.get("dataset_id", "?"))
        ds_id = ds.get("dataset_id", ds.get("id", "?"))
        lines.append(f"• *{_escape_md(str(name))}*")
        lines.append(f"  └ ID: `{_escape_md(str(ds_id))}`")
        lines.append(f"  └ Launch: `/run {ds_id}`")
        lines.append("")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return

    # Try to read background worker log file
    log_lines = []

    # Check for worker log in state dir
    bg_log = WORKER_STATE_DIR / "background.log"
    if bg_log.exists():
        try:
            content = bg_log.read_text(encoding="utf-8", errors="replace")
            log_lines = content.splitlines()[-30:]
        except Exception:
            pass

    # If no file log, use in-memory buffer
    if not log_lines:
        log_lines = list(LOG_BUFFER)[-30:]

    if not log_lines:
        await update.message.reply_text("📭 No logs available yet.")
        return

    text = "\n".join(log_lines)
    if len(text) > 3800:
        text = text[-3800:]

    await update.message.reply_text(f"📋 *Recent Logs:*\n```\n{text}\n```", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("🔍 Running full diagnosis...")
    data = _run_tool("diagnose", timeout=90)

    if "_raw" in data:
        raw = data["_raw"] or data.get("_stderr", "No output")
        # Send as plain text because diagnosis output has special chars
        await msg.edit_text(f"```\n{raw[:3800]}\n```", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await msg.edit_text(_format_status(data), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current LLM configuration status."""
    if not _auth_check(update):
        return

    gateway_url = os.getenv("MINE_GATEWAY_BASE_URL", "") or os.getenv("OPENCLAW_GATEWAY_BASE_URL", "")
    gateway_token = os.getenv("MINE_GATEWAY_TOKEN", "") or os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
    gateway_model = os.getenv("MINE_GATEWAY_MODEL", "") or os.getenv("OPENCLAW_GATEWAY_MODEL", "")
    enrich_mode = os.getenv("MINE_ENRICH_MODE", "") or os.getenv("OPENCLAW_ENRICH_MODE", "auto")

    lines = ["🤖 *LLM Configuration*\n"]

    # Gateway URL
    if gateway_url:
        lines.append(f"✅ *Gateway URL:* `{_escape_md(gateway_url)}`")
    else:
        lines.append("❌ *Gateway URL:* Not set")

    # Token
    if gateway_token:
        masked = gateway_token[:8] + "..." + gateway_token[-4:] if len(gateway_token) > 12 else "***"
        lines.append(f"✅ *API Token:* `{_escape_md(masked)}`")
    else:
        lines.append("❌ *API Token:* Not set")

    # Model
    if gateway_model:
        lines.append(f"✅ *Model:* `{_escape_md(gateway_model)}`")
    else:
        lines.append("⚪ *Model:* Default \\(openclaw/default\\)")

    # Enrich mode
    lines.append(f"ℹ️ *Enrich Mode:* `{_escape_md(enrich_mode or 'auto')}`")

    # Overall status
    lines.append("")
    if gateway_url and gateway_token:
        lines.append("✅ LLM enrichment is *enabled*")
        lines.append("PoW challenges \\& data enrichment will use this LLM")
    else:
        lines.append("⚠️ LLM enrichment is *disabled*")
        lines.append("Mining will work but without LLM\\-powered enrichment")
        lines.append("\nSet `MINE_GATEWAY_BASE_URL` and `MINE_GATEWAY_TOKEN` in \\.env")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retrieve and show the wallet address."""
    if not _auth_check(update):
        return
    msg = await update.message.reply_text("🔎 Fetching wallet address...")
    
    # Use isolated env to avoid exit -6 crash
    iso = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8"
    }
    wallet_bin_dir = str(Path.home() / ".local" / "bin")
    if os.path.exists(wallet_bin_dir):
        iso["PATH"] = f"{wallet_bin_dir}:{iso['PATH']}"
    if os.getenv("AWP_WALLET_TOKEN"):
        iso["AWP_WALLET_TOKEN"] = os.getenv("AWP_WALLET_TOKEN")

    try:
        result = subprocess.run([AWP_WALLET_BIN, "receive"], capture_output=True, text=True, timeout=30, env=iso)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            addr = data.get("address") or data.get("eoaAddress") or ""
            if not addr and data.get("addresses"):
                first = data["addresses"][0]
                addr = first.get("address") or first.get("eoaAddress")
            
            if addr:
                text = (
                    "👛 *Your AWP Wallet*\n\n"
                    f"Address: `{_escape_md(addr)}`\n\n"
                    "Tap to copy the address above\\."
                )
                await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await msg.edit_text("❌ Could not find address in wallet output\\.")
        else:
            await msg.edit_text(f"❌ Wallet error (exit {result.returncode}):\n`{_escape_md(result.stderr[:200])}`", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text(f"❌ Error: `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perform protocol onboarding (registration)."""
    uid = update.effective_user.id if update.effective_user else "unknown"
    logger.info(f"Command /onboard received from user {uid}")
    
    if not _auth_check(update):
        logger.warning(f"Auth check failed for user {uid} on /onboard")
        return
        
    if not update.message:
        logger.error("No message object in /onboard update")
        return

    msg = await update.message.reply_text("🚀 Starting gasless onboarding...")
    logger.info("Onboarding message sent, starting script...")
    
    token = os.getenv("AWP_WALLET_TOKEN")
    if not token:
        await msg.edit_text("❌ AWP_WALLET_TOKEN not set in .env. Run /wallet to check if you have one.")
        return

    # Use isolated env
    iso = {
        "PATH": f"/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONPATH": str(BASE_DIR / "benchmark_worknet" / "awp-skill" / "scripts")
    }
    
    script_path = BASE_DIR / "benchmark_worknet" / "awp-skill" / "scripts" / "relay-onboard.py"
    if not script_path.exists():
        await msg.edit_text("❌ Onboarding script not found. Did the clone fail?")
        return

    cmd = [_python_bin(), str(script_path), "--token", token]
    
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=iso)
        if proc.returncode == 0:
            logger.info("Onboarding script succeeded")
            await msg.edit_text(f"✅ *Onboarding Success\!*\n\n```\n{_escape_md(proc.stdout[:3800])}\n```", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            logger.error(f"Onboarding script failed with code {proc.returncode}")
            await msg.edit_text(f"❌ *Onboarding Failed\!* (exit {proc.returncode}):\n```\n{_escape_md(proc.stderr[:1000] or proc.stdout[:1000])}\n```", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.exception("Exception during onboarding")
        await msg.edit_text(f"❌ Error during onboarding: `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_bench(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Benchmark worker logs."""
    if not _auth_check(update):
        return
    
    log_file = "/tmp/awp_worker.log"
    if not os.path.exists(log_file):
        await update.message.reply_text("💤 Benchmark worker log not found. Is it running?")
        return
        
    try:
        result = subprocess.run(["tail", "-n", "20", log_file], capture_output=True, text=True, timeout=5)
        text = result.stdout.strip() or "Log is empty."
        await update.message.reply_text(f"📋 *Benchmark Worker Logs*\n\n```\n{_escape_md(text[:3800])}\n```", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading logs: `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_scores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Benchmark scores and earnings."""
    if not _auth_check(update):
        return
    
    q_file = "/tmp/awp_my_questions.json"
    a_file = "/tmp/awp_my_assignments.json"
    
    lines = ["📊 *Benchmark Earnings \(\$aBench\)*\n"]
    
    try:
        if os.path.exists(a_file):
            with open(a_file, "r") as f:
                data = json.load(f)
                items = data.get("data", {}).get("items", []) if isinstance(data, dict) else []
                total = len(items)
                scored = len([i for i in items if i.get("score", 0) > 0])
                avg = sum([i.get("score", 0) for i in items]) / (scored or 1)
                lines.append(f"• *Assignments Solved:* `{total}`")
                lines.append(f"• *Average Score:* `{avg:.2f}`")
        else:
            lines.append("• Assignments: `No data yet`")

        if os.path.exists(q_file):
            with open(q_file, "r") as f:
                data = json.load(f)
                items = data.get("data", {}).get("items", []) if isinstance(data, dict) else []
                total = len(items)
                lines.append(f"• *Questions Submitted:* `{total}`")
        
        lines.append(f"\n_Check again in 5 minutes for updates\._")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading scores: `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_miner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show live Mining logs from the latest session."""
    if not _auth_check(update):
        return
    
    # Try to find the latest log in output/agent-runs/
    log_dir = BASE_DIR / "output" / "agent-runs"
    if not log_dir.exists():
        await update.message.reply_text("💤 No mining output directory found yet.")
        return
        
    try:
        # Get the most recent .log file
        logs = list(log_dir.glob("*.log"))
        if not logs:
            await update.message.reply_text("💤 No mining logs found. Is the miner running?")
            return
            
        latest_log = max(logs, key=os.path.getmtime)
        
        result = subprocess.run(["tail", "-n", "20", str(latest_log)], capture_output=True, text=True, timeout=5)
        text = result.stdout.strip() or "Log is empty."
        
        # ESCAPE the header because it contains parentheses and dots
        header = _escape_md(f"⛏️ Mining Activity ({latest_log.name})")
        await update.message.reply_text(f"*{header}*\n\n```\n{_escape_md(text[:3800])}\n```", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading miner logs: `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop all active miners and benchmark workers."""
    if not _auth_check(update):
        return
        
    status_msg = await update.message.reply_text("🛑 Stopping all workers\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    
    try:
        # Stop both possible workers
        subprocess.run(["pm2", "stop", "awp-miner-v2"], capture_output=True)
        subprocess.run(["pm2", "stop", "awp-benchmark"], capture_output=True)
        
        await status_msg.edit_text("✅ All mining and benchmark workers have been *Stopped*.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await status_msg.edit_text(f"❌ Error stopping: `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch worknet (1-5) and toggle appropriate workers (ASYNCHRONOUS)."""
    if not _auth_check(update):
        return
        
    if not context.args:
        await update.message.reply_text("❓ Usage: `/switch <id> [amount]`\nExample: `/switch 1 100` or `/switch 2`")
        return
        
    wn_id = context.args[0]
    amount = context.args[1] if len(context.args) > 1 else ("10" if wn_id == "1" else "1")
    lock_days = "30" if wn_id == "1" else "1"
    
    status_msg = await update.message.reply_text(f"⏳ Switching to *Worknet {wn_id}* \\(Allocating {amount} AWP\\.\\.\\.\\)", parse_mode=ParseMode.MARKDOWN_V2)
    
    try:
        # Use absolute path to venv python if possible
        py_bin = str(BASE_DIR / ".venv" / "bin" / "python")
        if not os.path.exists(py_bin):
            py_bin = sys.executable

        # USE THE NEW RESILIENT SWITCH SCRIPT
        script_path = str(BASE_DIR / "scripts" / "remote_switch.py")
        env = os.environ.copy()
        token = os.environ.get("AWP_WALLET_TOKEN", "")
        
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            py_bin, script_path, 
            wn_id, amount, token,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            error_text = stderr.decode().strip() or stdout.decode().strip()
            await status_msg.edit_text(f"❌ Allocation failed:\n```\n{_escape_md(error_text)}\n```", parse_mode=ParseMode.MARKDOWN_V2)
            return

        # 2. Toggle PM2 workers (non-critical, can be sync)
        if wn_id == "1":
            subprocess.run(["pm2", "stop", "awp-miner-v2"], capture_output=True)
            subprocess.run(["pm2", "start", "scripts/run_benchmark.sh", "--name", "awp-benchmark"], capture_output=True, cwd=BASE_DIR)
            final_text = "✅ Switched to *Worknet 1 (Benchmark)*. Mesin kuis dinyalakan."
        else:
            subprocess.run(["pm2", "stop", "awp-benchmark"], capture_output=True)
            # Use run-worker persistent mode
            subprocess.run(["pm2", "delete", "awp-miner-v2"], capture_output=True)
            subprocess.run(["pm2", "start", f"{py_bin} scripts/run_tool.py -- run-worker 60 0", "--name", "awp-miner-v2"], capture_output=True, cwd=BASE_DIR)
            final_text = f"✅ Switched to *Worknet {wn_id} (Mining)*. Mesin tambang dinyalakan."
            
        await status_msg.edit_text(final_text, parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Error during switch: `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to see bot environment."""
    if not _auth_check(update):
        return
    
    # Collect key env vars
    keys = ["HOME", "PATH", "LANG", "LC_ALL", "USER", "SHELL", "AWP_WALLET_TOKEN", "OPENSSL_CONF"]
    lines = ["🌐 *Bot Runtime Environment*\n"]
    for k in keys:
        val = os.environ.get(k, "Not set")
        if "TOKEN" in k and val != "Not set":
            val = val[:8] + "..."
        lines.append(f"• `{k}`: `{_escape_md(val)}`")
    
    lines.append(f"\n• `sys.executable`: `{_escape_md(sys.executable)}`")
    
    # Test Node.js connectivity using the SAME isolated logic as _run_tool
    iso = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8"
    }
    wallet_bin_dir = str(Path.home() / ".local" / "bin")
    if os.path.exists(wallet_bin_dir):
        iso["PATH"] = f"{wallet_bin_dir}:{iso['PATH']}"

    try:
        node_check = subprocess.run(["node", "-e", "console.log(require('crypto').getCurves().length)"], 
                                    capture_output=True, text=True, timeout=5, env=iso)
        if node_check.returncode == 0:
            lines.append(f"\n• `Node Crypto Test`: ✅ Success `{node_check.stdout.strip()} curves`")
        else:
            lines.append(f"\n• `Node Crypto Test`: ❌ Failed (exit {node_check.returncode})")
    except Exception as e:
        lines.append(f"\n• `Node Crypto Test`: ⚠ `Error: {_escape_md(str(e))}`")
    
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


# ── Callback Query Handler (inline buttons) ──────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
        
    data = query.data
    user_id = query.from_user.id if query.from_user else "Unknown"
    
    # Use standard print for extreme visibility in PM2 logs
    print(f"DEBUG: Button clicked! User: {user_id}, Data: {data}")
    logger.info(f"🔘 Button clicked by {user_id}: {data}")
    
    try:
        await query.answer()
    except Exception as e:
        logger.error(f"Failed to answer query: {e}")

    # Auth check
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logger.warning(f"🚫 Unauthorized attempt from {user_id}")
        await query.edit_message_text(f"⛔ Unauthorized (ID: {user_id})")
        return

    try:
        if data == "status":
            await query.edit_message_text("🔄 Checking status...")
            result = _run_tool("agent-status")
            await query.edit_message_text(_format_status(result), parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "doctor":
            await query.edit_message_text("🔬 Running diagnostics...")
            result = _run_tool("doctor", timeout=60)
            if "_raw" in result:
                await query.edit_message_text(f"```\n{result['_raw'][:3800]}\n```", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                status = result.get("status", "unknown")
                emoji = "✅" if status == "ok" else "❌"
                text = f"{emoji} Doctor: `{_escape_md(status)}`"
                checks = result.get("checks", [])
                for c in checks:
                    icon = "✅" if c.get("ok") else "❌"
                    text += f"\n  {icon} {_escape_md(c.get('name', '?'))}"
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "start":
            await query.edit_message_text("⛏ Starting mining agent...")
            result = _run_tool("agent-start", timeout=180)
            state = result.get("state", "")
            if state == "selection_required":
                datasets = result.get("_internal", {}).get("datasets", [])
                lines = [
                    "📋 *Available Datasets:*",
                    "Send command to start:\n"
                ]
                for ds in datasets[:10]:
                    ds_id = str(ds.get("dataset_id") or ds.get("id") or "")
                    name = str(ds.get("name") or ds_id)
                    lines.append(f"• {name}: `/run {ds_id}`")
                    
                lines.append(f"\nTap or click a command above to copy it\\.")
                await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.edit_message_text(_format_status(result), parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "stop":
            await query.edit_message_text("⏹ Stopping mining...")
            result = _run_tool("agent-control", "stop", timeout=60)
            await query.edit_message_text(_format_status(result), parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "pause":
            await query.edit_message_text("⏸ Pausing mining...")
            result = _run_tool("agent-control", "pause", timeout=60)
            await query.edit_message_text(_format_status(result), parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "resume":
            await query.edit_message_text("▶️ Resuming mining...")
            result = _run_tool("agent-control", "resume", timeout=60)
            await query.edit_message_text(_format_status(result), parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "validator_start":
            await query.edit_message_text("🔍 Starting validator...")
            result = _run_tool("validator-start", timeout=180)
            await query.edit_message_text(_format_status(result), parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "logs":
            log_lines = []
            bg_log = WORKER_STATE_DIR / "background.log"
            if bg_log.exists():
                try:
                    content = bg_log.read_text(encoding="utf-8", errors="replace")
                    log_lines = content.splitlines()[-20:]
                except Exception:
                    pass
            if not log_lines:
                log_lines = list(LOG_BUFFER)[-20:]
            if log_lines:
                text = "\n".join(log_lines)[-3500:]
                await query.edit_message_text(f"📋 *Logs:*\n```\n{text}\n```", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.edit_message_text("📭 No logs available yet.")

        elif data.startswith("dataset_"):
            ds_id = data.replace("dataset_", "")
            await query.edit_message_text(f"⛏ Starting mining for dataset `{_escape_md(ds_id)}`\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
            result = _run_tool("agent-start", ds_id, timeout=180)
            await query.edit_message_text(_format_status(result), parse_mode=ParseMode.MARKDOWN_V2)

        elif data == "llm":
            gateway_url = os.getenv("MINE_GATEWAY_BASE_URL", "") or os.getenv("OPENCLAW_GATEWAY_BASE_URL", "")
            gateway_token = os.getenv("MINE_GATEWAY_TOKEN", "") or os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
            gateway_model = os.getenv("MINE_GATEWAY_MODEL", "") or os.getenv("OPENCLAW_GATEWAY_MODEL", "")
            enrich_mode = os.getenv("MINE_ENRICH_MODE", "") or os.getenv("OPENCLAW_ENRICH_MODE", "auto")

            lines = ["🤖 *LLM Configuration*\n"]
            lines.append(f"{'✅' if gateway_url else '❌'} *Gateway:* `{_escape_md(gateway_url or 'Not set')}`")
            if gateway_token:
                masked = gateway_token[:8] + "..." if len(gateway_token) > 8 else "***"
                lines.append(f"✅ *Token:* `{_escape_md(masked)}`")
            else:
                lines.append("❌ *Token:* Not set")
            lines.append(f"{'✅' if gateway_model else '⚪'} *Model:* `{_escape_md(gateway_model or 'default')}`")
            lines.append(f"ℹ️ *Mode:* `{_escape_md(enrich_mode or 'auto')}`")
            lines.append("")
            if gateway_url and gateway_token:
                lines.append("✅ LLM enrichment *enabled*")
            else:
                lines.append("⚠️ LLM enrichment *disabled*")
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error in button_handler: {e}", exc_info=True)
        try:
            await query.edit_message_text(f"❌ *Error in handler:* `{_escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pass


# ── Post-init: set bot commands ───────────────────────────────────

async def post_init(application: Application):
    # CRITICAL: Force clear any existing webhook to ensure polling works
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook cleared successfully.")
    except Exception as e:
        logger.warning(f"Failed to clear webhook: {e}")
        
    await application.bot.set_my_commands([
        BotCommand("start", "Show menu & help"),
        BotCommand("status", "Check agent status"),
        BotCommand("doctor", "Run environment diagnostics"),
        BotCommand("run", "Start mining"),
        BotCommand("stop", "Stop mining"),
        BotCommand("pause", "Pause mining"),
        BotCommand("resume", "Resume mining"),
        BotCommand("validator", "Start validator"),
        BotCommand("datasets", "List available datasets"),
        BotCommand("wallet", "Show wallet address"),
        BotCommand("onboard", "Register agent on AWP network"),
        BotCommand("switch", "Switch worknet (1-5)"),
        BotCommand("stop", "Stop all miners/workers"),
        BotCommand("miner", "Check Worknet 2 Mining status"),
        BotCommand("bench", "Check Benchmark worker status"),
        BotCommand("scores", "Show $aBench earnings"),
        BotCommand("logs", "Show recent logs"),
        BotCommand("diagnose", "Full diagnosis"),
        BotCommand("llm", "Check LLM configuration"),
        BotCommand("env", "Check bot environment"),
    ])
    logger.info("Bot commands registered successfully.")


# ── Main ──────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set. Check your .env file.")
        sys.exit(1)

    logger.info("Starting AWP Mine Telegram Bot...")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers (Callback first to be safe)
    app.add_handler(CallbackQueryHandler(button_handler))
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("validator", cmd_validator))
    app.add_handler(CommandHandler("datasets", cmd_datasets))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("onboard", cmd_onboard))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("miner", cmd_miner))
    app.add_handler(CommandHandler("bench", cmd_bench))
    app.add_handler(CommandHandler("scores", cmd_scores))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("diagnose", cmd_diagnose))
    app.add_handler(CommandHandler("llm", cmd_llm))
    app.add_handler(CommandHandler("env", cmd_env))

    logger.info("Bot is polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
