"""
Tests that the Sniper sell flow's price selection correctly routes to
Yellowstone (when enabled) vs. the existing Shyft path (unchanged default),
and propagates the fetched price unmodified.

GEYSER_USE_PERSISTENT_STREAM defaults to true, so get_sniper_sell_price()
calls get_yellowstone_price_cached() (the persistent-stream-backed path) by
default; these tests patch that name. The GEYSER_USE_PERSISTENT_STREAM=false
legacy (one-shot get_yellowstone_price()) escape hatch is covered at the
bottom of this file.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import autosnipe_sell_script as sell_script
from yellowstone_pricing import YellowstonePricingError

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"


@pytest.fixture(autouse=True)
def _clear_yellowstone_cooldown():
    sell_script._yellowstone_cooldown.clear()
    yield
    sell_script._yellowstone_cooldown.clear()


def test_uses_yellowstone_when_enabled(capsys):
    yellowstone_result = {"usdPrice": 1.23, "usd_price": 1.23, "source": "yellowstone_grpc_accountSubscribe"}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script, "get_yellowstone_price_cached", return_value=yellowstone_result
    ) as yellowstone_fetch, patch.object(sell_script, "get_token_price") as shyft_fetch:
        result = sell_script.get_sniper_sell_price(PUMP_MINT)

    yellowstone_fetch.assert_called_once_with(PUMP_MINT)
    shyft_fetch.assert_not_called()
    # Price is propagated unmodified into the sell decision.
    assert result["usdPrice"] == 1.23
    output = capsys.readouterr().out
    assert "Successfully retrieved" in output
    assert PUMP_MINT in output
    assert "usd_price=1.23" in output
    assert "source=yellowstone_grpc_accountSubscribe" in output


def test_falls_back_to_shyft_when_yellowstone_disabled():
    shyft_result = {"usdPrice": 4.56, "usd_price": 4.56, "source": "shyft_rpc_pumpfun_bonding_curve"}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=False), patch.object(
        sell_script, "get_yellowstone_price_cached"
    ) as yellowstone_fetch, patch.object(
        sell_script, "get_token_price", return_value=shyft_result
    ) as shyft_fetch:
        result = sell_script.get_sniper_sell_price(PUMP_MINT)

    shyft_fetch.assert_called_once_with(PUMP_MINT, include_token_details=False)
    yellowstone_fetch.assert_not_called()
    assert result["usdPrice"] == 4.56


def test_yellowstone_failure_falls_back_to_shyft():
    """
    A token that has stopped trading will never get a pushed Yellowstone
    update (accountSubscribe times out forever), so a Yellowstone failure
    must fall back to a Shyft RPC snapshot read instead of skipping the
    trade indefinitely.
    """
    shyft_result = {"usdPrice": 7.89, "usd_price": 7.89, "source": "shyft_rpc_pumpfun_bonding_curve"}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script,
        "get_yellowstone_price_cached",
        side_effect=YellowstonePricingError("unavailable"),
    ) as yellowstone_fetch, patch.object(
        sell_script, "get_token_price", return_value=shyft_result
    ) as shyft_fetch, patch.object(sell_script, "logger", MagicMock()):
        result = sell_script.get_sniper_sell_price(PUMP_MINT)

    yellowstone_fetch.assert_called_once_with(PUMP_MINT)
    shyft_fetch.assert_called_once_with(PUMP_MINT, include_token_details=False)
    assert result["usdPrice"] == 7.89


def test_yellowstone_failure_puts_mint_on_cooldown_skipping_next_call():
    """
    Regression: against ~30 real open (quiet) positions, re-attempting
    Yellowstone on every sell-cycle tick made a full price pass take 20-30s
    and exhausted the account's gRPC connection limit. A mint that just
    timed out must skip straight to Shyft on the next call, not retry.
    """
    shyft_result = {"usdPrice": 7.89, "usd_price": 7.89, "source": "shyft_rpc_pumpfun_bonding_curve"}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script, "get_yellowstone_price_cached", side_effect=YellowstonePricingError("unavailable"),
    ) as yellowstone_fetch, patch.object(
        sell_script, "get_token_price", return_value=shyft_result
    ) as shyft_fetch, patch.object(sell_script, "logger", MagicMock()):
        sell_script.get_sniper_sell_price(PUMP_MINT)  # times out, starts the cooldown
        result = sell_script.get_sniper_sell_price(PUMP_MINT)  # must skip Yellowstone this time

    yellowstone_fetch.assert_called_once()  # only the first call attempted it
    assert shyft_fetch.call_count == 2
    assert result["usdPrice"] == 7.89


def test_yellowstone_cooldown_is_per_mint():
    other_mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    shyft_result = {"usdPrice": 1.0, "usd_price": 1.0}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script, "get_yellowstone_price_cached", side_effect=YellowstonePricingError("unavailable"),
    ) as yellowstone_fetch, patch.object(
        sell_script, "get_token_price", return_value=shyft_result
    ), patch.object(sell_script, "logger", MagicMock()):
        sell_script.get_sniper_sell_price(PUMP_MINT)  # PUMP_MINT now on cooldown
        sell_script.get_sniper_sell_price(other_mint)  # a different mint must still try

    assert yellowstone_fetch.call_count == 2


def test_yellowstone_cooldown_expires_and_retries():
    shyft_result = {"usdPrice": 1.0, "usd_price": 1.0}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script, "get_yellowstone_price_cached", side_effect=YellowstonePricingError("unavailable"),
    ) as yellowstone_fetch, patch.object(
        sell_script, "get_token_price", return_value=shyft_result
    ), patch.object(sell_script, "logger", MagicMock()), patch.object(
        sell_script, "SNIPER_SELL_YELLOWSTONE_COOLDOWN_SEC", 0.0
    ):
        sell_script.get_sniper_sell_price(PUMP_MINT)
        sell_script.get_sniper_sell_price(PUMP_MINT)  # cooldown already elapsed (0s)

    assert yellowstone_fetch.call_count == 2


def test_yellowstone_success_clears_a_stale_cooldown_entry():
    """A mint can start trading again after its cooldown window has elapsed
    (dict entry still present, just expired); a successful fetch must clear it
    so the next failure doesn't look like a repeat and extend it further."""
    yellowstone_result = {"usdPrice": 1.23, "usd_price": 1.23, "source": "yellowstone_grpc_accountSubscribe"}
    sell_script._yellowstone_cooldown[PUMP_MINT] = sell_script.time.monotonic() - 1  # already expired

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script, "get_yellowstone_price_cached", return_value=yellowstone_result
    ):
        sell_script.get_sniper_sell_price(PUMP_MINT)

    assert PUMP_MINT not in sell_script._yellowstone_cooldown


def test_yellowstone_and_shyft_both_failing_propagates():
    """If the Shyft fallback itself fails too, the scheduler loop's generic
    except-Exception branch is relied on to skip-and-retry."""
    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script,
        "get_yellowstone_price_cached",
        side_effect=YellowstonePricingError("unavailable"),
    ), patch.object(
        sell_script, "get_token_price", side_effect=RuntimeError("shyft also down")
    ), patch.object(sell_script, "logger", MagicMock()):
        with pytest.raises(RuntimeError):
            sell_script.get_sniper_sell_price(PUMP_MINT)


def test_geyser_use_persistent_stream_false_uses_legacy_one_shot_path():
    """Escape hatch: GEYSER_USE_PERSISTENT_STREAM=false must route through
    the old get_yellowstone_price() one-shot call, not the cached one, so a
    deploy can revert to pre-persistent-stream behavior without a code
    change if the new path ever needs to be backed out."""
    yellowstone_result = {"usdPrice": 1.23, "usd_price": 1.23, "source": "yellowstone_grpc_accountSubscribe"}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script, "GEYSER_USE_PERSISTENT_STREAM", False
    ), patch.object(
        sell_script, "get_yellowstone_price", return_value=yellowstone_result
    ) as one_shot, patch.object(
        sell_script, "get_yellowstone_price_cached"
    ) as cached, patch.object(sell_script, "get_token_price") as shyft_fetch:
        result = sell_script.get_sniper_sell_price(PUMP_MINT)

    one_shot.assert_called_once_with(PUMP_MINT)
    cached.assert_not_called()
    shyft_fetch.assert_not_called()
    assert result["usdPrice"] == 1.23
