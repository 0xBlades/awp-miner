"""EIP-712 signing via awp-wallet CLI subprocess."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit

from common import (
    DEFAULT_EIP712_CHAIN_ID,
    DEFAULT_EIP712_DOMAIN_NAME,
    DEFAULT_EIP712_VERIFYING_CONTRACT,
    WALLET_SESSION_DURATION_SECONDS,
    persist_wallet_session,
)

from eip712_primitives import (
    DEFAULT_SIGNED_HEADERS,
    keccak_hex as _keccak_hex,
    hash_query as _hash_query,
    hash_headers as _hash_headers,
    hash_body as _hash_body,
)


class WalletSigner:
    """Bridge to awp-wallet CLI for EIP-712 request signing."""

    def __init__(self, wallet_bin: str = "awp-wallet", session_token: str = "") -> None:
        self._bin = wallet_bin
        self._token = session_token
        self._signer_address: str | None = None

    @property
    def session_token(self) -> str:
        return self._token

    def _run(self, *args: str) -> dict[str, Any]:
        cmd = [self._bin, *args]
        # CRITICAL: Build a CLEAN environment for awp-wallet.
        # 'os.environ.copy()' inherits PM2's internal vars (NODE_APP_INSTANCE,
        # PM2_HOME, etc.) which crash Node.js 22 native bindings with exit -6.
        from pathlib import Path
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME") or os.environ.get("USERPROFILE", ""),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "NODE_NO_WARNINGS": "1",
            "USER": os.environ.get("USER", ""),
        }
        # Pass all AWP_* vars through (token, wallet bin, etc.)
        for key, val in os.environ.items():
            if key.startswith("AWP_"):
                env[key] = val
        # Ensure local bin dir is in PATH for awp-wallet to be found
        local_bin = str(Path.home() / ".local" / "bin")
        if local_bin not in env["PATH"]:
            env["PATH"] = f"{local_bin}:{env['PATH']}"

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Invalid or expired session token" in stderr:
                raise RuntimeError(
                    "The auto-managed wallet session expired or is invalid; rerun "
                    "`awp-wallet unlock --duration 3600` or rerun bootstrap before retrying. "
                    f"awp-wallet stderr: {stderr}"
                )
            raise RuntimeError(f"awp-wallet failed (exit {result.returncode}): {stderr}")
        return json.loads(result.stdout)

    def get_address(self) -> str:
        if self._signer_address is None:
            resp = self._run("receive")
            addr = resp.get("address") or resp.get("eoaAddress") or ""
            if not addr:
                addresses = resp.get("addresses")
                if isinstance(addresses, list) and addresses:
                    first = addresses[0]
                    if isinstance(first, dict):
                        addr = first.get("address", "") or first.get("eoaAddress", "")
            if not addr:
                raise RuntimeError("awp-wallet receive did not return an address")
            self._signer_address = addr
        return self._signer_address

    def sign_typed_data(self, typed_data: dict[str, Any]) -> str:
        resp = self._run(
            "sign-typed-data",
            "--token",
            self._token,
            "--data",
            json.dumps(typed_data, separators=(",", ":")),
        )
        sig = resp.get("signature", "")
        if not sig:
            raise RuntimeError("awp-wallet sign-typed-data returned empty signature")
        return sig

    def renew_session(self, *, duration_seconds: int = WALLET_SESSION_DURATION_SECONDS) -> dict[str, int | str]:
        issued_at = int(time.time())
        resp = self._run("unlock", "--duration", str(max(1, duration_seconds)))
        session_token = str(resp.get("sessionToken") or "").strip()
        if not session_token:
            raise RuntimeError("awp-wallet unlock did not return sessionToken")
        self._token = session_token
        os.environ["AWP_WALLET_TOKEN"] = session_token
        expires_at = issued_at + max(1, duration_seconds)
        os.environ["AWP_WALLET_TOKEN_EXPIRES_AT"] = str(expires_at)
        persist_wallet_session(session_token, expires_at=expires_at)
        return {
            "session_token": session_token,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }

    def build_typed_data(
        self,
        *,
        method: str,
        url: str,
        body: Any,
        content_type: str,
        now: int,
        nonce: int,
        chain_id: int = DEFAULT_EIP712_CHAIN_ID,
        domain_name: str = DEFAULT_EIP712_DOMAIN_NAME,
        domain_version: str = "1",
        verifying_contract: str = DEFAULT_EIP712_VERIFYING_CONTRACT,
        signed_headers: tuple[str, ...] = DEFAULT_SIGNED_HEADERS,
    ) -> dict[str, Any]:
        split = urlsplit(url)
        request_headers = {
            "content-type": content_type,
        }

        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "APIRequest": [
                    {"name": "method", "type": "string"},
                    {"name": "host", "type": "string"},
                    {"name": "path", "type": "string"},
                    {"name": "queryHash", "type": "bytes32"},
                    {"name": "headersHash", "type": "bytes32"},
                    {"name": "bodyHash", "type": "bytes32"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "issuedAt", "type": "uint256"},
                    {"name": "expiresAt", "type": "uint256"},
                ],
            },
            "primaryType": "APIRequest",
            "domain": {
                "name": domain_name,
                "version": domain_version,
                "chainId": chain_id,
                "verifyingContract": verifying_contract,
            },
            "message": {
                "method": method.upper(),
                "host": split.netloc,
                "path": split.path or "/",
                "queryHash": _hash_query(url),
                "headersHash": _hash_headers(request_headers, signed_headers),
                "bodyHash": _hash_body(body, content_type),
                "nonce": nonce,
                "issuedAt": now,
                "expiresAt": now + 300,
            },
        }

    def build_auth_headers(
        self,
        method: str,
        url: str,
        body: Any = None,
        *,
        content_type: str = "application/json",
        chain_id: int = DEFAULT_EIP712_CHAIN_ID,
        domain_name: str = DEFAULT_EIP712_DOMAIN_NAME,
        domain_version: str = "1",
        verifying_contract: str = DEFAULT_EIP712_VERIFYING_CONTRACT,
    ) -> dict[str, str]:
        now = int(time.time())
        nonce = secrets.randbits(52)  # 52-bit int, safe for all JSON parsers
        nonce_str = str(nonce)
        typed_data = self.build_typed_data(
            method=method,
            url=url,
            body=body,
            content_type=content_type,
            now=now,
            nonce=nonce,
            chain_id=chain_id,
            domain_name=domain_name,
            domain_version=domain_version,
            verifying_contract=verifying_contract,
        )
        signature = self.sign_typed_data(typed_data)
        message = typed_data["message"]
        return {
            "Content-Type": content_type,
            "X-Signer": self.get_address(),
            "X-Signature": signature if signature.startswith("0x") else f"0x{signature}",
            "X-Nonce": nonce_str,
            "X-Issued-At": str(message["issuedAt"]),
            "X-Expires-At": str(message["expiresAt"]),
            "X-Chain-Id": str(chain_id),
            "X-Signed-Headers": ",".join(DEFAULT_SIGNED_HEADERS),
        }
