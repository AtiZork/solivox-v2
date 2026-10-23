"""Unit tests for yellowstone_pricing (mocked yellowstone_cache - no live Yellowstone endpoint)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import yellowstone_pricing as yp
from shyft_pricing import ShyftPricingService

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"


def _bonding_raw(vt: int = 1_000_000_000_000, vs: int = 30_000_000_000, complete: bool = False) -> bytes:
    raw = bytearray(64)
    raw[8:16] = int(vt).to_bytes(8, "little")
    raw[16:24] = int(vs).to_bytes(8, "little")
    raw[48] = 1 if complete else 0
    return bytes(raw)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Most tests want USE_GEYSER enabled with a fake endpoint."""
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")


@pytest.fixture(autouse=True)
def _no_cache_by_default(monkeypatch):
    """
    yellowstone_cache starts "empty" for every test unless a test explicitly
    overrides these - keeps each test's cache-hit/cache-miss behavior
    explicit rather than depending on real module-level cache state (which
    would otherwise leak between tests sharing PUMP_MINT).
    """
    monkeypatch.setattr(yp, "get_cached_curve_raw", lambda pda: None)
    monkeypatch.setattr(yp, "get_cached_pool_metadata", lambda mint: None)
    monkeypatch.setattr(yp, "set_cached_curve_raw", lambda pda, raw: None)


@pytest.fixture
def shared_service(monkeypatch):
    """
    Mock the shared ShyftPricingService singleton used for the pre-subscribe
    migration check, SOL/USD conversion, the PumpSwap fallback, and (new) the
    RPC-seed-on-cache-miss read. Defaults to "not migrated" / sol_usd=200.0
    so existing bonding-curve-path tests don't need to know about the
    migration pre-check at all; tests that care can override
    svc.is_bonding_curve_migrated / svc.get_pumpswap_pool_price / svc._get_account_bytes.
    """
    svc = MagicMock()
    svc.is_bonding_curve_migrated.return_value = False
    svc.get_sol_usd.return_value = 200.0
    monkeypatch.setattr(yp, "get_shyft_pricing_service", lambda: svc)
    return svc


def test_disabled_raises_when_use_geyser_false(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", False)
    assert yp.is_yellowstone_enabled() is False
    with pytest.raises(yp.YellowstonePricingError):
        yp.get_yellowstone_price(PUMP_MINT)


def test_disabled_raises_when_url_missing(monkeypatch):
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "")
    assert yp.is_yellowstone_enabled() is False
    with pytest.raises(yp.YellowstonePricingError):
        yp.get_yellowstone_price(PUMP_MINT)


def test_missing_mint_raises():
    with pytest.raises(yp.YellowstonePricingError):
        yp.get_yellowstone_price("   ")


def test_valid_price_response(shared_service):
    raw = _bonding_raw()
    with patch.object(yp, "_fetch_bonding_curve_account", return_value=raw) as fetch:
        result = yp.get_yellowstone_price(PUMP_MINT)

    fetch.assert_called_once()
    assert result["mint"] == PUMP_MINT
    assert result["source"] == "yellowstone_grpc_cached"
    assert result["sol_price_usd"] == pytest.approx(200.0)
    expected_usd = ShyftPricingService._price_sol_from_bonding_raw(raw) * 200.0
    assert result["usd_price"] == pytest.approx(expected_usd)
    assert result["usdPrice"] == result["usd_price"]
    assert result["timestamp_unix"] > 0


def test_invalid_zero_price_raises(shared_service):
    zero_reserves = _bonding_raw(vt=0, vs=0)
    with patch.object(yp, "_fetch_bonding_curve_account", return_value=zero_reserves):
        with pytest.raises(yp.YellowstonePricingError):
            yp.get_yellowstone_price(PUMP_MINT)


def test_already_migrated_skips_subscribe_and_uses_pumpswap(shared_service):
    """Pre-check catches an already-migrated curve without touching the curve cache at all."""
    shared_service.is_bonding_curve_migrated.return_value = True
    shared_service.get_pumpswap_pool_price.return_value = {
        "mint": PUMP_MINT,
        "bonding_curve": None,
        "pumpswap_pool": "Pool1111111111111111111111111111111111111",
        "price_in_sol": 0.00002,
        "usd_price": 0.003,
        "sol_price_usd": 150.0,
        "source": "shyft_rpc_pumpswap_pool",
    }
    with patch.object(yp, "_fetch_bonding_curve_account") as fetch:
        result = yp.get_yellowstone_price(PUMP_MINT)

    fetch.assert_not_called()  # never touched the curve cache/RPC for an already-dead curve
    shared_service.get_pumpswap_pool_price.assert_called_once_with(PUMP_MINT, cached_pool_metadata=None)
    assert result["source"] == "shyft_rpc_pumpswap_pool"
    assert result["usdPrice"] == pytest.approx(0.003)


def test_already_migrated_uses_cached_pool_metadata_when_available(shared_service):
    """When yellowstone_cache already has this mint's pool, it's passed straight through -
    this is what actually closes the real "no PumpSwap pool found" gap."""
    shared_service.is_bonding_curve_migrated.return_value = True
    shared_service.get_pumpswap_pool_price.return_value = {
        "mint": PUMP_MINT, "bonding_curve": None, "pumpswap_pool": "Pool111...",
        "price_in_sol": 0.00002, "usd_price": 0.003, "sol_price_usd": 150.0,
        "source": "shyft_rpc_pumpswap_pool",
    }
    cached_pool = {
        "pool_pubkey": "Pool1111111111111111111111111111111111111",
        "base_mint": PUMP_MINT,
        "quote_mint": "So11111111111111111111111111111111111111112",
        "base_vault": "BaseVault11111111111111111111111111111111",
        "quote_vault": "QuoteVault1111111111111111111111111111111",
        "virtual_quote_reserves": 1_000_000_000,
    }
    with patch.object(yp, "get_cached_pool_metadata", lambda mint: cached_pool):
        yp.get_yellowstone_price(PUMP_MINT)

    shared_service.get_pumpswap_pool_price.assert_called_once_with(
        PUMP_MINT, cached_pool_metadata=cached_pool
    )


def test_migrates_during_subscription_falls_back_to_pumpswap(shared_service):
    """complete=true arrives from the cache (pre-check said not-yet-migrated) -> fall back, don't raise."""
    migrated = _bonding_raw(complete=True)
    shared_service.get_pumpswap_pool_price.return_value = {
        "mint": PUMP_MINT,
        "bonding_curve": None,
        "pumpswap_pool": "Pool1111111111111111111111111111111111111",
        "price_in_sol": 0.00002,
        "usd_price": 0.003,
        "sol_price_usd": 150.0,
        "source": "shyft_rpc_pumpswap_pool",
    }
    with patch.object(yp, "_fetch_bonding_curve_account", return_value=migrated):
        result = yp.get_yellowstone_price(PUMP_MINT)

    shared_service.get_pumpswap_pool_price.assert_called_once_with(PUMP_MINT, cached_pool_metadata=None)
    assert result["source"] == "shyft_rpc_pumpswap_pool"


def test_pumpswap_fallback_failure_raises_instead_of_stale_price(shared_service):
    """If PumpSwap pricing also fails, must still never return a stale bonding-curve price."""
    migrated = _bonding_raw(complete=True)
    shared_service.get_pumpswap_pool_price.side_effect = Exception("no pool found")
    with patch.object(yp, "_fetch_bonding_curve_account", return_value=migrated):
        with pytest.raises(yp.YellowstonePricingError, match="migrated"):
            yp.get_yellowstone_price(PUMP_MINT)


def test_missing_sol_usd_raises(shared_service):
    raw = _bonding_raw()
    shared_service.get_sol_usd.return_value = None
    with patch.object(yp, "_fetch_bonding_curve_account", return_value=raw):
        with pytest.raises(yp.YellowstonePricingError):
            yp.get_yellowstone_price(PUMP_MINT)


# --- _fetch_bonding_curve_account itself: cache-hit / cache-miss-seeds-via-RPC ---


def test_fetch_returns_cached_value_without_any_rpc(shared_service):
    raw = _bonding_raw()
    with patch.object(yp, "get_cached_curve_raw", return_value=raw):
        data = yp._fetch_bonding_curve_account("SomePda111111111111111111111111")

    assert data == raw
    shared_service._get_account_bytes.assert_not_called()


def test_fetch_seeds_via_one_rpc_call_on_cache_miss_and_writes_it_back(shared_service):
    raw = _bonding_raw()
    shared_service._get_account_bytes.return_value = raw
    with patch.object(yp, "set_cached_curve_raw") as set_cached:
        data = yp._fetch_bonding_curve_account("SomePda111111111111111111111111")

    assert data == raw
    shared_service._get_account_bytes.assert_called_once_with("SomePda111111111111111111111111")
    set_cached.assert_called_once_with("SomePda111111111111111111111111", raw)


def test_fetch_raises_when_cache_miss_and_rpc_seed_also_fails(shared_service):
    shared_service._get_account_bytes.return_value = None
    with pytest.raises(yp.YellowstonePricingError):
        yp._fetch_bonding_curve_account("SomePda111111111111111111111111")
