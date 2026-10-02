"""
Tests that the Sniper sell flow's price selection correctly routes to
Yellowstone (when enabled) vs. the existing Shyft path (unchanged default),
and propagates the fetched price unmodified.
"""

from __future__ import annotations

from unittest.mock import patch

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


def test_yellowstone_failure_propagates_as_yellowstone_error():
    """The scheduler loop relies on this to skip-and-retry rather than sell on a bad price."""
    with patch.object(sell_script, "is_yellowstone_enabled", return_value=True), patch.object(
        sell_script,
        "get_yellowstone_price",
        side_effect=YellowstonePricingError("unavailable"),
    ):
        with pytest.raises(YellowstonePricingError):
            sell_script.get_sniper_sell_price(PUMP_MINT)
