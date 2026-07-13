#!/usr/bin/env python3
"""
Verify Phase 1 sniper WebSocket setup without starting the full Flask app.

Usage:
  python scripts/verify_sniper_ws.py
  python scripts/verify_sniper_ws.py --listen 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

# Project root on path
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check_settings() -> bool:
    from settings import SOLANA_RPC_URL, SNIPER_USE_WEBSOCKET, get_solana_ws_urls, solana_client

    ws_urls = get_solana_ws_urls()
    print("--- Settings ---")
    print(f"  SOLANA_RPC_URL      = {SOLANA_RPC_URL}")
    print(f"  SOLANA_WS_URLS      = {ws_urls}")
    print(f"  SNIPER_USE_WEBSOCKET= {SNIPER_USE_WEBSOCKET}")
    ok = all(isinstance(u, str) and u.startswith("ws") for u in ws_urls)
    if not ok:
        print("  FAIL: all WS URLs must be ws:// or wss:// strings")
        return False
    print("  OK: WebSocket URL list configured")

    try:
        slot = solana_client.get_slot()
        print(f"  OK: RPC reachable — current slot {slot.value}")
    except Exception as exc:
        print(f"  WARN: RPC not reachable ({exc})")
    return True


async def test_ws_subscribe(listen_seconds: float) -> bool:
    from settings import PUMP_FUN_PROGRAM_ID_STR, get_solana_ws_urls
    from websockets import connect

    ws_urls = get_solana_ws_urls()
    ws_url = ws_urls[-1]  # test public fallback last (most likely to work in dev)

    print("\n--- WebSocket logsSubscribe test ---")
    print(f"  Connecting to {ws_url} ...")

    try:
        async with connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [PUMP_FUN_PROGRAM_ID_STR]},
                    {"commitment": "processed"},
                ],
            }
            await ws.send(json.dumps(req))
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            resp = json.loads(raw)
            sub_id = resp.get("result")
            if not sub_id:
                print(f"  FAIL: subscribe response: {resp}")
                return False
            print(f"  OK: subscribed (id={sub_id})")

            if listen_seconds <= 0:
                return True

            print(f"  Listening {listen_seconds}s for Pump.fun log events...")
            deadline = time.time() + listen_seconds
            count = 0
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
                    data = json.loads(msg)
                    if "params" in data:
                        count += 1
                        sig = data.get("params", {}).get("result", {}).get("value", {}).get("signature", "")
                        logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                        kind = "?"
                        if any("Instruction: Create" in l for l in logs):
                            kind = "Create"
                        elif any("Instruction: Buy" in l for l in logs):
                            kind = "Buy"
                        print(f"    event #{count} [{kind}] sig={str(sig)[:20]}...")
                except asyncio.TimeoutError:
                    break

            print(f"  OK: received {count} log notification(s) in {listen_seconds}s")
            return True
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def test_sniper_stream_module() -> bool:
    print("\n--- sniper_stream module ---")
    try:
        from sniper_stream import BuyEvent, SniperStreamState, get_mint_buy_stats, stream_state

        t = SniperStreamState()
        t.register_launch("TestMintpump", "sig123")
        t.add_buy(
            "TestMintpump",
            BuyEvent(signature="s", sol_amount=1.0, usd_value=150.0, timestamp=time.time()),
        )
        stats = t.get_buy_stats("TestMintpump", 80)
        assert stats["qualifying_count"] == 1
        print("  OK: in-memory counter logic works")
        print(f"  stream_state status: {stream_state.status()}")
        return True
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify sniper WebSocket Phase 1 setup")
    parser.add_argument("--listen", type=float, default=15, help="Seconds to listen for events (0=skip)")
    args = parser.parse_args()

    results = [
        check_settings(),
        test_sniper_stream_module(),
        asyncio.run(test_ws_subscribe(args.listen)),
    ]

    print("\n=== Summary ===")
    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print("SOME CHECKS FAILED — see output above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
