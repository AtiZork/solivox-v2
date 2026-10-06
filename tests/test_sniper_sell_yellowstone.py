"""
Tests that the Sniper sell flow's price selection correctly routes to
Yellowstone (when enabled) vs. the existing Shyft path (unchanged default),
and propagates the fetched price unmodified.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import autosnipe_sell_script as sell_script
from yellowstone_pricing import YellowstonePricingError

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"


def test_uses_yellowstone_when_enabled(capsys):
    yellowstone_result = {"usdPrice": 1.23, "usd_price": 1.23, "source": "yellowstone_grpc_accountSubscribe"}

    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script, "get_yellowstone_price", return_value=yellowstone_result
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
        sell_script, "get_yellowstone_price"
    ) as yellowstone_fetch, patch.object(
        sell_script, "get_token_price", return_value=shyft_result
    ) as shyft_fetch:
        result = sell_script.get_sniper_sell_price(PUMP_MINT)

    shyft_fetch.assert_called_once_with(PUMP_MINT)
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
        "get_yellowstone_price",
        side_effect=YellowstonePricingError("unavailable"),
    ) as yellowstone_fetch, patch.object(
        sell_script, "get_token_price", return_value=shyft_result
    ) as shyft_fetch, patch.object(sell_script, "logger", MagicMock()):
        result = sell_script.get_sniper_sell_price(PUMP_MINT)

    yellowstone_fetch.assert_called_once_with(PUMP_MINT)
    shyft_fetch.assert_called_once_with(PUMP_MINT)
    assert result["usdPrice"] == 7.89


def test_yellowstone_and_shyft_both_failing_propagates():
    """If the Shyft fallback itself fails too, the scheduler loop's generic
    except-Exception branch is relied on to skip-and-retry."""
    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script,
        "get_yellowstone_price",
        side_effect=YellowstonePricingError("unavailable"),
    ), patch.object(
        sell_script, "get_token_price", side_effect=RuntimeError("shyft also down")
    ), patch.object(sell_script, "logger", MagicMock()):
        with pytest.raises(RuntimeError):
            sell_script.get_sniper_sell_price(PUMP_MINT)
