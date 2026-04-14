"""OpenClaw CLI wrapper for LLM calls.

Optimized based on example-worker.py patterns:
- Dedicated agent per validator instance (multi-instance safe)
- Session purge before each call (prevents context overflow)
- Popen with graceful timeout (abortable, not blocking)
- Rate limit detection and backoff
- Resolved binary path at init (nohup/background compat)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("validator.llm")

DEFAULT_TIMEOUT = 120

# Module-level state (guarded by _module_lock for thread safety)
_agent_id: str = ""
_openclaw_bin: str = "openclaw"
_rate_limit_until: float = 0
_initialized: bool = False

import threading as _threading
_module_lock = _threading.Lock()


def _resolve_openclaw_path() -> str:
    """Find the absolute path to the openclaw binary."""
    global _openclaw_bin

    # Try common binary names including user's 'hermes'
    for name in ["hermes", "openclaw", "openclaw.mjs"]:
        path = shutil.which(name)
        if path:
            _openclaw_bin = path
            log.info("binary found: %s", path)
            return _openclaw_bin

    search_dirs = [
        os.path.expanduser("~/.local/bin"), # Where hermes usually lives
        "/usr/local/bin",
        os.path.expanduser("~/.openclaw/bin"),
        os.path.expanduser("~/bin"),
        os.path.expanduser("~/.openclaw"),
        "/usr/bin",
    ]
    for d in search_dirs:
        for name in ["hermes", "openclaw", "openclaw.mjs"]:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                _openclaw_bin = candidate
                log.info("binary found at: %s", candidate)
                return _openclaw_bin

    log.warning("no reasoning binary found in PATH or common locations")
    return _openclaw_bin


def _agent_exists(agent_id: str) -> bool:
    """Check if an OpenClaw agent exists."""
    try:
        result = subprocess.run(
            [_openclaw_bin, "agents", "list"],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0 and agent_id in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _ensure_agent(agent_id: str) -> None:
    """Create the dedicated agent if it does not exist."""
    if _agent_exists(agent_id):
        log.info("using existing agent: %s", agent_id)
        return

    log.info("creating agent: %s", agent_id)
    try:
        result = subprocess.run(
            [
                _openclaw_bin, "agents", "add", agent_id,
                "--workspace", os.path.expanduser(f"~/.openclaw/workspace-{agent_id}"),
                "--non-interactive",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log.info("created agent: %s", agent_id)
        else:
            log.warning("failed to create agent: %s", result.stderr.strip()[:200])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("openclaw not available for agent creation")


def _purge_agent_sessions() -> None:
    """Delete all session transcripts to prevent context overflow.

    OpenClaw session structure:
      ~/.openclaw/agents/{agent_id}/sessions/
        sessions.json   (index)
        {uuid}.jsonl     (transcript — grows unbounded)
        {uuid}.jsonl.lock
    """
    if not _agent_id:
        return
    session_dir = Path.home() / ".openclaw" / "agents" / _agent_id / "sessions"
    if not session_dir.is_dir():
        return
    count = 0
    for f in session_dir.iterdir():
        try:
            if f.name == "sessions.json":
                f.write_text("{}")
                count += 1
            elif f.suffix in (".jsonl", ".lock"):
                f.unlink()
                count += 1
        except OSError:
            pass
    if count > 0:
        log.info("purged %d session files for agent %s", count, _agent_id)


def init(instance_id: str = "") -> str:
    """Initialize the OpenClaw integration. Call once at startup.

    Args:
        instance_id: Optional suffix for multi-instance isolation (e.g. wallet address).

    Returns:
        The agent ID that will be used for LLM calls.
    """
    global _agent_id, _initialized

    with _module_lock:
        _resolve_openclaw_path()

        suffix = f"-{instance_id}" if instance_id else ""
        _agent_id = f"mine-validator{suffix}"

        _ensure_agent(_agent_id)
        _purge_agent_sessions()

        # Verify agent was created successfully
        if not _agent_exists(_agent_id):
            log.warning("agent %s not available after creation attempt — CLI calls may fail", _agent_id)

        _initialized = True
    return _agent_id


def call_openclaw(
    prompt: str,
    *,
    cli_path: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Call OpenClaw agent CLI with a prompt and return the response.

    - Creates a dedicated agent on first call (if not initialized via init())
    - Purges session files before each call
    - Uses Popen for graceful timeout (abortable)
    - Detects rate limits and backs off

    Args:
        prompt: The prompt to send to the LLM.
        cli_path: Ignored (kept for backward compat). Uses resolved path.
        timeout: Timeout in seconds.

    Returns:
        Raw text response from the LLM.

    Raises:
        RuntimeError: If the CLI is not available or fails.
        TimeoutError: If the call times out.
    """
    global _rate_limit_until

    with _module_lock:
        if not _initialized:
            init()

        # Rate limit backoff
        if time.monotonic() < _rate_limit_until:
            remaining = int(_rate_limit_until - time.monotonic())
            raise RuntimeError(f"rate limit backoff, {remaining}s remaining")

        # Snapshot module-level state under the lock to avoid TOCTOU
        agent_id = _agent_id
        bin_path = _openclaw_bin

    _purge_agent_sessions()

    try:
        proc = subprocess.Popen(
            [bin_path, "agent", "--agent", agent_id, "--message", prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"OpenClaw CLI not found at '{_openclaw_bin}'") from exc

    # Use communicate() with timeout to avoid pipe deadlock
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        _purge_agent_sessions()
        raise TimeoutError(f"OpenClaw CLI timeout ({timeout}s)")

    _purge_agent_sessions()

    if proc.returncode == 0 and stdout.strip():
        text = stdout.strip()
        # Try to extract from structured agent response
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "output" in data:
                extracted = _extract_text_from_agent_response(data)
                if extracted:
                    return extracted
        except json.JSONDecodeError:
            pass
        return text

    # Handle errors
    err = stderr.strip() if stderr else ""
    if err:
        log.warning("OpenClaw CLI stderr: %s", err[:200])

    # Detect rate limit
    if "429" in err or "rate" in err.lower() or "Extra usage" in err:
        with _module_lock:
            _rate_limit_until = time.monotonic() + 60
        log.warning("rate limit detected, backing off 60s")

    raise RuntimeError(f"OpenClaw CLI failed (exit {proc.returncode}): {err[:200]}")


def _extract_text_from_agent_response(data: dict[str, Any]) -> str | None:
    """Extract text from a structured agent response."""
    for item in reversed(data.get("output", [])):
        for block in reversed(item.get("content", [])):
            if "text" in block:
                return block["text"]
        if "text" in item:
            return item["text"]
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return msg.get("content")
    return None


def parse_json_response(response: str) -> dict[str, Any]:
    """Extract JSON object from LLM response.

    Handles markdown code fences and embedded JSON in free text.
    """
    stripped = response.strip()

    # Strip markdown code fences
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        inner = "\n".join(lines[1:])
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3]
        stripped = inner.strip()

    # Try direct parse
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Find first { ... } in text
    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    matches = re.findall(json_pattern, response, re.DOTALL)
    for match in matches:
        try:
            result = json.loads(match)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    log.warning("Could not parse JSON from response: %s", response[:200])
    return {}
