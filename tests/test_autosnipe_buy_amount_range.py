"""Tests for the optional max-USD ceiling on qualifying buy transactions.

Client request: the buy-qualification check only ever had a floor
(buy_txns_over_80_usd, ">= $X") - a token with a few oversized wash-trade
buys could clear that floor easily, which was flagged as a likely reason
scam launches were still passing the filter. This adds an optional ceiling
(buy_txns_max_usd) so a qualifying buy is [min, max] instead of [min, inf).
None/unset preserves the exact previous floor-only behavior - every test
below that omits max_usd_value is checking that backward compatibility.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosnipe_buy_logic import usd_value_in_range, validate_buy_amount_range
from sniper_stream import BuyEvent, SniperStreamState


# --- usd_value_in_range (the core new logic, used by every call site) ---

def test_no_ceiling_matches_old_floor_only_behavior():
    assert usd_value_in_range(80, min_usd_value=80, max_usd_value=None) is True
    assert usd_value_in_range(1_000_000, min_usd_value=80, max_usd_value=None) is True
    assert usd_value_in_range(79.99, min_usd_value=80, max_usd_value=None) is False


def test_ceiling_excludes_values_above_it():
    assert usd_value_in_range(150, min_usd_value=100, max_usd_value=200) is True
    assert usd_value_in_range(200, min_usd_value=100, max_usd_value=200) is True  # inclusive
    assert usd_value_in_range(200.01, min_usd_value=100, max_usd_value=200) is False


def test_ceiling_still_enforces_floor():
    assert usd_value_in_range(99.99, min_usd_value=100, max_usd_value=200) is False


def test_client_example_strategies():
    # "10 transactions of $100-$200"
    assert usd_value_in_range(150, 100, 200) is True
    assert usd_value_in_range(250, 100, 200) is False
    # "15 transactions between $30-$120"
    assert usd_value_in_range(60, 30, 120) is True
    assert usd_value_in_range(29, 30, 120) is False
    assert usd_value_in_range(121, 30, 120) is False


# --- validate_buy_amount_range (cross-field config validation) ---

def test_max_below_min_is_rejected():
    assert validate_buy_amount_range(100, 50) != []


def test_max_at_or_above_min_is_accepted():
    assert validate_buy_amount_range(100, 100) == []
    assert validate_buy_amount_range(100, 200) == []


def test_either_bound_missing_skips_cross_check():
    assert validate_buy_amount_range(100, None) == []
    assert validate_buy_amount_range(None, 200) == []


# --- SniperStreamState.get_buy_stats (WS in-memory counters) ---

def _tracker_with_buys(values):
    state = SniperStreamState()
    mint = "TestMint"
    now = time.time()
    state.mint_registry[mint] = SimpleNamespace(
        buys=[BuyEvent(signature=f"sig{i}", sol_amount=1.0, usd_value=v, timestamp=now)
              for i, v in enumerate(values)],
        launch_time=now,
        candidates_seen=len(values),
        candidates_excluded=0,
    )
    return state, mint


def test_get_buy_stats_without_ceiling_counts_everything_over_floor():
    state, mint = _tracker_with_buys([50, 90, 500, 10_000])
    stats = state.get_buy_stats(mint, min_usd_value=80)
    assert stats["qualifying_count"] == 3  # 90, 500, 10000


def test_get_buy_stats_with_ceiling_excludes_oversized_buys():
    state, mint = _tracker_with_buys([50, 90, 500, 10_000])
    stats = state.get_buy_stats(mint, min_usd_value=80, max_usd_value=600)
    assert stats["qualifying_count"] == 2  # 90, 500 - the 10000 wash-trade-style buy is excluded now


def test_get_buy_stats_unknown_mint_unaffected_by_new_param():
    state = SniperStreamState()
    stats = state.get_buy_stats("NeverSeen", min_usd_value=80, max_usd_value=200)
    assert stats["qualifying_count"] == 0


# --- should_buy_token WS path: confirms the config field actually reaches
# get_mint_buy_stats, not just that the pure function works in isolation ---

def _config(**kwargs):
    base = dict(
        user_id=1,
        active=True,
        launch_delay=1,
        min_txns=2,
        buy_txns_over_80_usd=100,
        buy_txns_max_usd=200,
        half_txns_scan_enabled=False,
        half_txns_scan_duration=30,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


@patch("autosnipe_buy_new_token.time.sleep", return_value=None)
@patch("sniper_stream.is_ws_active", return_value=True)
@patch("sniper_stream.get_mint_buy_stats")
def test_should_buy_token_passes_configured_ceiling_to_ws_stats(mock_stats, _ws_active, _sleep):
    from autosnipe_buy_new_token import should_buy_token

    mock_stats.return_value = {
        "total_buys": 3, "qualifying_count": 2, "launch_time": time.time(),
        "qualifying_buys": [], "candidates_seen": 3, "candidates_excluded": 0,
    }
    config = _config()
    with patch("autosnipe_buy_new_token.time.time", side_effect=[1000.0, 1000.0, 1000.1]):
        result = should_buy_token("Mint", 1, config)

    assert result is True
    # Must be called with both the floor AND the configured ceiling - a
    # regression here would silently fall back to floor-only behavior.
    mock_stats.assert_called_with("Mint", 100, 200)
