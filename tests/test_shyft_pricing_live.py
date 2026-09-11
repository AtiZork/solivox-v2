"""Live Shyft integration tests (require network + valid API key)."""

from __future__ import annotations

import os
import threading
import time

import pytest

from shyft_pricing import ShyftPricingError, get_shyft_pricing_service

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"
WSOL_MINT = "So11111111111111111111111111111111111111112"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def service():
    return get_shyft_pricing_service()


def test_shyft_token_details_and_price_real_mint(service):
    details = service.get_token_details(PUMP_MINT)
    assert details.get("mint") == PUMP_MINT
    # Name/symbol may be empty for some mints; address must round-trip
    assert "raw" in details

    snap = service.get_latest_price(PUMP_MINT)
    assert snap.mint == PUMP_MINT
    assert snap.timestamp
    assert snap.timestamp_unix > 0
    assert snap.price_in_sol is not None and snap.price_in_sol > 0
    # USD may depend on Pyth parse; prefer present
    if snap.usd_price is not None:
        assert snap.usd_price > 0


def test_shyft_second_mint_wsol(service):
    snap = service.get_latest_price(WSOL_MINT)
    assert snap.mint == WSOL_MINT
    assert snap.usd_price is not None and snap.usd_price > 0
    assert snap.source


def test_shyft_ws_stream_initial_update(service):
    updates = []
    done = threading.Event()

    def on_update(snap):
        updates.append(snap)
        done.set()

    thread = service.stream_price(PUMP_MINT, on_update, max_updates=1, run_in_thread=True)
    ok = done.wait(timeout=60)
    service.stop_stream(PUMP_MINT)
    if thread:
        thread.join(timeout=15)

    assert ok, "timed out waiting for Shyft stream/RPC initial update"
    assert updates
    assert updates[0].price_in_sol is not None or updates[0].usd_price is not None
