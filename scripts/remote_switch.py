#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add the submodule to path so we can import awp_lib
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR / "benchmark_worknet" / "awp-skill" / "scripts"))

try:
    import awp_lib
except ImportError:
    print(json.dumps({"error": "Could not find awp_lib in benchmark_worknet/awp-skill/scripts"}))
    sys.exit(1)

def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: remote_switch.py <worknet_id> <amount> [token]"}))
        sys.exit(1)

    wn_id = sys.argv[1]
    amount = sys.argv[2]
    token = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("AWP_WALLET_TOKEN", "")

    # PATCH get_wallet_address logic locally in this script
    # We call awp-wallet receive and ignore exit code if we get JSON
    try:
        py_bin = sys.executable
        wallet_bin = os.environ.get("AWP_WALLET_BIN", "awp-wallet")
        
        # Manually run receive to bypass the crash in awp_lib
        res = subprocess.run([wallet_bin, "receive", "--chain", "base"], capture_output=True, text=True)
        # Even if it returns 1, we try to parse the output
        try:
            addr_data = json.loads(res.stdout.strip())
            wallet_addr = addr_data.get("eoaAddress")
        except:
            # Fallback to awp_lib if my manual check fails (maybe it works there)
            wallet_addr = awp_lib.get_wallet_address()
            
        if not wallet_addr:
            print(json.dumps({"error": "Failed to get wallet address"}))
            sys.exit(1)

        # Now run the allocation logic using awp_lib components
        # (This mirrors relay-allocate.py but skips the internal get_wallet_address crash)
        
        awp_lib.step("fetch_registry")
        registry = awp_lib.get_registry()
        
        awp_lib.step("expand_worknet")
        worknet_id = awp_lib.expand_worknet_id(int(wn_id))
        
        awp_lib.step("convert_amount")
        amount_wei = awp_lib.to_wei(amount)
        
        # Get AWPAllocator contract from registry
        allocator_addr = awp_lib.require_contract(registry, "awpAllocator")
        
        awp_lib.step("fetch_nonce")
        nonce = awp_lib.get_onchain_nonce(allocator_addr, wallet_addr)
        
        domain = awp_lib.get_eip712_domain(registry, "AWPAllocator")
        deadline = int(time.time()) + 3600
        
        awp_lib.step("build_eip712")
        eip712_data = awp_lib.build_eip712(
            domain,
            "Allocate",
            [
                {"name": "staker", "type": "address"},
                {"name": "agent", "type": "address"},
                {"name": "worknetId", "type": "uint256"},
                {"name": "amount", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
            {
                "staker": wallet_addr,
                "agent": wallet_addr, # We allocate to ourselves (Agent Mode)
                "worknetId": worknet_id,
                "amount": amount_wei,
                "nonce": nonce,
                "deadline": deadline,
            },
        )
        
        awp_lib.step("sign_eip712")
        #signature = awp_lib.wallet_sign_typed_data(token, eip712_data)
        
        # MANUALLY HANDLE SIGNING TO IGNORE EXIT CODE 1
        sign_args = [wallet_bin, "sign-typed-data", "--data", json.dumps(eip712_data), "--chain", "base"]
        if token:
            sign_args += ["--token", token]
            
        sign_res = subprocess.run(sign_args, capture_output=True, text=True)
        try:
            sign_data = json.loads(sign_res.stdout.strip())
            signature = sign_data.get("signature")
        except:
            print(json.dumps({"error": "Failed to parse signature JSON", "stdout": sign_res.stdout, "stderr": sign_res.stderr}))
            sys.exit(1)
            
        if not signature:
            print(json.dumps({"error": "Empty signature returned", "stdout": sign_res.stdout}))
            sys.exit(1)
        
        relay_endpoint = f"{awp_lib.RELAY_BASE}/relay/allocate"
        relay_body = {
            "chainId": domain["chainId"],
            "staker": wallet_addr,
            "agent": wallet_addr,
            "worknetId": str(worknet_id),
            "amount": str(amount_wei),
            "deadline": deadline,
            "signature": signature
        }
        
        awp_lib.step("submit_relay")
        http_code, body = awp_lib.api_post(relay_endpoint, relay_body)
        
        if 200 <= http_code < 300:
            print(json.dumps({"status": "success", "worknet": wn_id, "amount": amount}))
        else:
            print(json.dumps({"error": f"Relay HTTP {http_code}", "detail": body}))
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
