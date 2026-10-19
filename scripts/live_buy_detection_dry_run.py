#!/usr/bin/env python3
"""
Real-time, read-only verification of the aggregator-routing buy-detection
fix against LIVE Pump.fun mainnet data. Starts the actual sniper_stream
WebSocket listener (the same code path production uses) and lets it run its
real Create/Buy detection pipeline unmodified - same throttle, same thread
pools, same _resolve_signer_buy logic.

Does NOT call should_buy_token and does NOT execute any trade or spend any
SOL - it only observes and reports. This measures two things live:

  1. Detection speed: launch_ws_to_registered / buy_ws_to_counted latency
     (same StageStats the bot logs in production as [latency-summary]).
  2. Miss-prevention: for every real buy counted, whether the OLD
     "Instruction: Buy"-only check would also have caught it, or whether it
     was only caught because of this fix (aggregator-routed).

Usage:
  python scripts/live_buy_detection_dry_run.py --seconds 240
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("dry_run")

import sniper_stream as ss  # noqa: E402

_old_would_have_caught = 0
_old_would_have_missed = 0
_new_total_counted = 0

_real_process_mint_buy = ss._process_mint_buy


def _instrumented_process_mint_buy(mint, signature, logs, received_at=None):
    global _old_would_have_caught, _old_would_have_missed, _new_total_counted
    had_old_marker = ss._logs_contain(logs, "Instruction: Buy")
    tracker_before = ss.stream_state.mint_registry.get(mint)
    before_count = len(tracker_before.buys) if tracker_before else 0

    _real_process_mint_buy(mint, signature, logs, received_at)

    tracker_after = ss.stream_state.mint_registry.get(mint)
    after_count = len(tracker_after.buys) if tracker_after else before_count
    if after_count > before_count:
        _new_total_counted += 1
        if had_old_marker:
            _old_would_have_caught += 1
        else:
            _old_would_have_missed += 1
            logger.info(
                "[FIX-PROVEN] mint=%s sig=%s... counted via aggregator-aware detection "
                "(old 'Instruction: Buy'-only check would have MISSED this one)",
                mint[:10], signature[:16],
            )


ss._process_mint_buy = _instrumented_process_mint_buy


def on_new_token(mint: str) -> None:
    logger.info("[detect] new token launch: %s", mint)


def main(observe_seconds: int) -> int:
    logger.info("Connecting to live Pump.fun WS feed (read-only, no trades, no SOL spent)...")
    started = ss.start_sniper_stream(on_new_token=on_new_token)
    if not started:
        logger.error("start_sniper_stream returned False (SNIPER_USE_WEBSOCKET=false?) - aborting.")
        return 1

    connected = ss.wait_for_connection(timeout_sec=20)
    logger.info("Connected: %s", connected)
    if not connected:
        logger.error("Could not connect to any configured WS endpoint - aborting.")
        return 1

    logger.info("Observing live launches/buys for %ds ...", observe_seconds)
    time.sleep(observe_seconds)

    logger.info("=== Observation window complete (%ds) ===", observe_seconds)
    logger.info("Latency summary: %s", ss.rpc_stats.snapshot())

    tracked = list(ss.stream_state.mint_registry.items())
    logger.info("Tokens launched during window: %d", len(tracked))
    for mint, tracker in tracked:
        stats = ss.get_mint_buy_stats(mint, min_usd_value=0)
        logger.info(
            "  %s: total_buys=%d candidates_seen=%d candidates_excluded=%d",
            mint, stats["total_buys"], stats["candidates_seen"], stats["candidates_excluded"],
        )

    logger.info(
        "=== Detection-fix result: %d buy(s) counted total | %d also catchable by OLD logic | "
        "%d ONLY caught because of this fix (aggregator-routed, old logic would have missed) ===",
        _new_total_counted, _old_would_have_caught, _old_would_have_missed,
    )

    ss.stop_sniper_stream()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=240)
    args = parser.parse_args()
    raise SystemExit(main(args.seconds))
