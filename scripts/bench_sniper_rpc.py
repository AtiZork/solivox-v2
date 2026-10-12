"""
Read-only capacity check for the sniper's getTransaction load, to decide
SNIPER_RPC_MIN_INTERVAL_SEC before lowering it. Sends no transactions and
changes nothing; run it on the server next to the node:

    .venv/bin/python scripts/bench_sniper_rpc.py
    .venv/bin/python scripts/bench_sniper_rpc.py --levels 1,2,4,8 --sigs 150 --demand-seconds 30

Reports, per concurrency level: calls/s, p50/p95/max latency and errors, and
the node's slot lag before/after (a growing lag means the load hurts the
validator). Also measures demand: how many Pump.fun buy transactions per
second the node's WebSocket delivers (an upper bound on what the sniper must
resolve — it only resolves buys for mints launched in the last
SNIPER_MINT_TRACKING_TTL_SEC).

The current default throttle (0.25s) allows 4 calls/s for all mints combined.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from settings import PUMP_FUN_PROGRAM_ID_STR, SNIPER_RPC_MIN_INTERVAL_SEC, SOLANA_RPC_URL, get_solana_ws_urls  # noqa: E402

MAX_CONCURRENCY = 16


def rpc(url, method, params, timeout=10):
    body = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout).json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def slot_lag(url):
    processed = rpc(url, "getSlot", [{"commitment": "processed"}])
    confirmed = rpc(url, "getSlot", [{"commitment": "confirmed"}])
    finalized = rpc(url, "getSlot", [{"commitment": "finalized"}])
    return {"processed": processed, "confirmed_lag": processed - confirmed, "finalized_lag": processed - finalized}


def bench_level(url, sigs, concurrency):
    latencies, errors = [], {}

    def one(sig):
        start = time.monotonic()
        try:
            result = rpc(url, "getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                      "maxSupportedTransactionVersion": 1}])
            if result is None:
                errors["not_found"] = errors.get("not_found", 0) + 1
        except Exception as exc:  # noqa: BLE001 - report every failure kind
            key = str(exc)[:60]
            errors[key] = errors.get(key, 0) + 1
        latencies.append((time.monotonic() - start) * 1000)

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, sigs))
    wall = time.monotonic() - start
    latencies.sort()
    return {
        "concurrency": concurrency,
        "calls_per_sec": round(len(sigs) / wall, 1),
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 1),
        "max_ms": round(latencies[-1], 1),
        "errors": errors,
    }


async def measure_demand(ws_url, seconds):
    from websockets import connect

    buys = creates = 0
    async with connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                  "params": [{"mentions": [PUMP_FUN_PROGRAM_ID_STR]}, {"commitment": "confirmed"}]}))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                break
            value = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
            logs = value.get("logs") or []
            if value.get("err") is not None:
                continue
            if any("Instruction: Buy" in line for line in logs):
                buys += 1
            if any(line.endswith("Instruction: Create") or line.endswith("Instruction: CreateV2") for line in logs):
                creates += 1
    return {"seconds": seconds, "buy_tx_per_sec": round(buys / seconds, 1), "launches_per_sec": round(creates / seconds, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", default=SOLANA_RPC_URL)
    parser.add_argument("--ws", default=(get_solana_ws_urls() or [""])[0])
    parser.add_argument("--sigs", type=int, default=150, help="signatures fetched per level (max 1000)")
    parser.add_argument("--levels", default="1,2,4,8", help=f"concurrency levels, each <= {MAX_CONCURRENCY}")
    parser.add_argument("--demand-seconds", type=int, default=30, help="0 skips the demand measurement")
    args = parser.parse_args()

    levels = [min(MAX_CONCURRENCY, max(1, int(x))) for x in args.levels.split(",")]
    print(f"RPC {args.rpc}   current SNIPER_RPC_MIN_INTERVAL_SEC={SNIPER_RPC_MIN_INTERVAL_SEC} "
          f"(cap {1 / SNIPER_RPC_MIN_INTERVAL_SEC if SNIPER_RPC_MIN_INTERVAL_SEC > 0 else float('inf'):.1f} calls/s)")
    print("health:", rpc(args.rpc, "getHealth", []))
    print("slot lag before:", slot_lag(args.rpc))

    if args.demand_seconds > 0 and args.ws:
        print("demand:", asyncio.run(measure_demand(args.ws, args.demand_seconds)))

    sigs = [s["signature"] for s in rpc(args.rpc, "getSignaturesForAddress",
                                         [PUMP_FUN_PROGRAM_ID_STR, {"limit": min(1000, args.sigs), "commitment": "confirmed"}])]
    print(f"benchmarking getTransaction on {len(sigs)} recent Pump.fun signatures")
    for level in levels:
        print(bench_level(args.rpc, sigs, level))
        time.sleep(1)
    print("slot lag after:", slot_lag(args.rpc))


if __name__ == "__main__":
    main()
