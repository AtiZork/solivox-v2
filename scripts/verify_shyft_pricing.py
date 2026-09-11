"""
Live integration checks against real Shyft endpoints.

Run:
  python scripts/verify_shyft_pricing.py
  pytest tests/test_shyft_pricing_live.py -m integration
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shyft_pricing import ShyftPricingError, get_shyft_pricing_service

# Well-known / project tokens
TOKENS = {
    # Default pump-style mint used elsewhere in this repo
    "pump_sample": "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump",
    # Wrapped SOL — priced via Pyth through Shyft RPC
    "wsol": "So11111111111111111111111111111111111111112",
}


def _print_snap(label: str, snap) -> None:
    data = snap.to_dict() if hasattr(snap, "to_dict") else snap
    # Never dump secrets; snapshot has none
    print(f"\n=== {label} ===")
    print(json.dumps(data, indent=2, default=str))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = get_shyft_pricing_service()
    print(f"Using Shyft network={service.network}")
    print("RPC/WS URLs redacted in service logs.")

    # 1) Token details + price for pump sample
    mint1 = TOKENS["pump_sample"]
    try:
        details = service.get_token_details(mint1)
        print("\n=== token details (mint1) ===")
        print(json.dumps({k: v for k, v in details.items() if k != "raw"}, indent=2, default=str))
    except ShyftPricingError as exc:
        print(f"Token details failed for {mint1}: {exc}")
        details = None

    try:
        snap1 = service.get_latest_price(mint1)
        _print_snap("latest price mint1", snap1)
    except ShyftPricingError as exc:
        print(f"FAIL: could not price mint1 {mint1}: {exc}")
        return 1

    if snap1.usd_price is None and snap1.price_in_sol is None:
        print("FAIL: no price fields returned")
        return 1
    if not snap1.timestamp:
        print("FAIL: missing timestamp")
        return 1

    # 2) Second token (WSOL) — dynamic mint handling
    mint2 = TOKENS["wsol"]
    try:
        snap2 = service.get_latest_price(mint2)
        _print_snap("latest price mint2 (WSOL)", snap2)
    except ShyftPricingError as exc:
        print(f"FAIL: could not price mint2 {mint2}: {exc}")
        return 1

    if not snap2.usd_price or snap2.usd_price <= 0:
        print("FAIL: WSOL USD price missing/invalid")
        return 1

    # 3) Live WS: wait briefly for connection + initial snapshot (and optional notify)
    updates: list = []
    done = threading.Event()

    def on_update(snap) -> None:
        updates.append(snap)
        print(f"LIVE update #{len(updates)} source={snap.source} usd={snap.usd_price} sol={snap.price_in_sol}")
        if len(updates) >= 1:
            done.set()

    print("\n=== starting short WS stream (mint1) ===")
    thread = service.stream_price(mint1, on_update, max_updates=2, run_in_thread=True)
    done.wait(timeout=45)
    time.sleep(2)
    service.stop_stream(mint1)
    if thread:
        thread.join(timeout=10)

    if not updates:
        print("WARN: no WS updates received within timeout (RPC snapshot may still be valid)")
    else:
        print(f"OK: received {len(updates)} stream update(s)")

    print("\nSUCCESS: Shyft live pricing verified with real mints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
