"""
Sniper buy counting from Pump.fun events, against real mainnet logs
(tests/fixtures_pump_logs.json, captured from token C7CK7gZZ...pump).
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from solders.pubkey import Pubkey

import sniper_stream as ss

FIX = json.loads((Path(__file__).parent / "fixtures_pump_logs.json").read_text())
MINT = FIX["mint"]
PUMP = ss.PUMP_FUN_PROGRAM_ID_STR


@pytest.fixture(autouse=True)
def fresh_state():
    state = ss.SniperStreamState()
    calls = []
    with patch.object(ss, "stream_state", state), \
         patch.object(ss, "_submit_logged", lambda pool, fn, *a: fn(*a)), \
         patch.object(ss, "_on_new_token", lambda mint: calls.append(mint)), \
         patch.object(ss, "_get_sol_usd_price", return_value=100.0):
        yield state, calls


def _trade_logs(mint: str, lamports: int, is_buy: bool, program: str = PUMP) -> list:
    raw = ss._TRADE_EVENT + bytes(Pubkey.from_string(mint)) + struct.pack("<QQ?", lamports, 123, is_buy) + bytes(64)
    return [
        f"Program {program} invoke [1]",
        "Program log: Instruction: Buy",
        "Program data: " + base64.b64encode(raw).decode(),
        f"Program {program} success",
    ]


# --- parsing real logs ----------------------------------------------------

def test_real_create_v2_logs_give_the_mint():
    assert ss.parse_create_mint(FIX["create"]["logs"]) == MINT


def test_real_version_1_buy_gives_exact_sol():
    buys = ss.parse_trade_buys(FIX["buy_v1"]["logs"])
    assert [m for m, _ in buys] == [MINT]
    assert buys[0][1] == pytest.approx(FIX["buy_v1"]["sol"], abs=1e-6)


def test_real_buy_paid_by_another_wallet_gives_exact_sol():
    """The old method (fee payer's balance drop) read this buy as ~0 SOL."""
    buys = ss.parse_trade_buys(FIX["buy_zero_feepayer"]["logs"])
    assert buys[0][1] == pytest.approx(FIX["buy_zero_feepayer"]["sol"], abs=1e-6)
    assert buys[0][1] > 0.01


def test_sell_events_are_not_counted_as_buys():
    assert ss.parse_trade_buys(_trade_logs(MINT, 5 * 10**8, is_buy=False)) == []


def test_events_from_other_programs_are_ignored():
    other = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
    assert ss.parse_trade_buys(_trade_logs(MINT, 5 * 10**8, True, program=other)) == []


def test_create_idempotent_is_not_a_launch():
    logs = ["Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL invoke [1]",
            "Program log: CreateIdempotent", "Program log: Instruction: CreateIdempotent",
            "Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL success"] + _trade_logs(MINT, 10**8, True)
    assert ss._is_create_tx(logs) is False
    assert ss.parse_create_mint(logs) is None


# --- stream handling ------------------------------------------------------

def test_launch_registers_immediately_and_starts_buy_check(fresh_state):
    state, calls = fresh_state
    ss._handle_program_logs(FIX["create"]["signature"], FIX["create"]["logs"])
    assert state.is_tracking(MINT)
    assert calls == [MINT]


def test_creator_buy_in_launch_tx_is_not_counted(fresh_state):
    state, _ = fresh_state
    ss._handle_program_logs(FIX["create"]["signature"], FIX["create"]["logs"])
    assert state.get_buy_stats(MINT, 0)["total_buys"] == 0


def test_buys_after_launch_are_counted_with_exact_usd(fresh_state):
    state, _ = fresh_state
    ss._handle_program_logs(FIX["create"]["signature"], FIX["create"]["logs"])
    ss._handle_program_logs(FIX["buy_v1"]["signature"], FIX["buy_v1"]["logs"])
    ss._handle_program_logs(FIX["buy_zero_feepayer"]["signature"], FIX["buy_zero_feepayer"]["logs"])

    stats = state.get_buy_stats(MINT, min_usd_value=0)
    assert stats["total_buys"] == 2
    usd = sorted(b.usd_value for b in stats["qualifying_buys"])
    assert usd == pytest.approx(sorted([FIX["buy_v1"]["sol"] * 100, FIX["buy_zero_feepayer"]["sol"] * 100]), abs=1e-3)


def test_qualifying_count_uses_usd_threshold(fresh_state):
    state, _ = fresh_state
    ss._handle_program_logs(FIX["create"]["signature"], FIX["create"]["logs"])
    for i, sol in enumerate([0.6, 0.4, 1.2, 0.2, 0.5]):  # $60, $40, $120, $20, $50 at $100/SOL
        ss._handle_program_logs(f"sig{i}", _trade_logs(MINT, int(sol * 1e9), True))
    assert state.get_buy_stats(MINT, min_usd_value=50)["qualifying_count"] == 3


def test_same_transaction_is_counted_once(fresh_state):
    state, _ = fresh_state
    ss._handle_program_logs(FIX["create"]["signature"], FIX["create"]["logs"])
    for _ in range(3):
        ss._handle_program_logs(FIX["buy_v1"]["signature"], FIX["buy_v1"]["logs"])
    assert state.get_buy_stats(MINT, 0)["total_buys"] == 1


def test_buys_of_untracked_tokens_are_ignored(fresh_state):
    state, _ = fresh_state
    ss._handle_program_logs(FIX["buy_v1"]["signature"], FIX["buy_v1"]["logs"])
    assert state.get_buy_stats(MINT, 0)["total_buys"] == 0
    assert FIX["buy_v1"]["signature"] not in state.seen_signatures


def test_non_pump_suffix_launch_skipped_by_default(fresh_state):
    state, calls = fresh_state
    other_mint = str(Pubkey.from_bytes(bytes(range(32))))
    assert not other_mint.endswith("pump")
    raw = ss._CREATE_EVENT + b"".join(struct.pack("<I", 1) + b"x" for _ in range(3)) + bytes(Pubkey.from_string(other_mint))
    logs = [f"Program {PUMP} invoke [1]", "Program log: Instruction: CreateV2",
            "Program data: " + base64.b64encode(raw).decode(), f"Program {PUMP} success"]

    ss._handle_program_logs("sigA", logs)
    assert not state.is_tracking(other_mint)

    with patch.object(ss, "SNIPER_REQUIRE_PUMP_SUFFIX", False):
        ss._handle_program_logs("sigB", logs)
    assert state.is_tracking(other_mint)
    assert calls == [other_mint]


def test_launch_without_event_falls_back_to_rpc_resolution(fresh_state):
    _, _ = fresh_state
    logs = [f"Program {PUMP} invoke [1]", "Program log: Instruction: CreateV2", f"Program {PUMP} success"]
    with patch.object(ss, "_extract_mint_from_create", return_value=MINT) as resolve:
        ss._handle_program_logs("sigC", logs)
    resolve.assert_called_once()
    assert ss.stream_state.is_tracking(MINT)


def test_buy_check_sees_counted_buys(fresh_state):
    """End-to-end through the real buy rule: 5 buys of >= $50 within the window."""
    state, _ = fresh_state
    ss._handle_program_logs(FIX["create"]["signature"], FIX["create"]["logs"])
    for i in range(5):
        ss._handle_program_logs(f"q{i}", _trade_logs(MINT, int(0.6e9), True))  # $60 each

    import autosnipe_buy_new_token as buy
    config = type("Cfg", (), dict(launch_delay=1, min_txns=5, buy_txns_over_80_usd=50,
                                  half_txns_scan_enabled=False, half_txns_scan_duration=30))()
    with patch.object(ss, "is_ws_active", return_value=True):
        assert buy.should_buy_token(MINT, 1, config) is True
