"""Unit tests for yellowstone_pricing (mocked gRPC — no live Yellowstone endpoint)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import grpc
import pytest

import yellowstone_pricing as yp
from shyft_pricing import ShyftPricingService
from yellowstone_grpc.generated import geyser_pb2

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"


def _bonding_raw(vt: int = 1_000_000_000_000, vs: int = 30_000_000_000, complete: bool = False) -> bytes:
    raw = bytearray(64)
    raw[8:16] = int(vt).to_bytes(8, "little")
    raw[16:24] = int(vs).to_bytes(8, "little")
    raw[48] = 1 if complete else 0
    return bytes(raw)


def _account_update(data: bytes) -> geyser_pb2.SubscribeUpdate:
    update = geyser_pb2.SubscribeUpdate()
    update.account.account.pubkey = b"\x00" * 32
    update.account.account.data = data
    update.account.slot = 1
    return update


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Most tests want USE_GEYSER enabled with a fake endpoint."""
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    monkeypatch.setattr(yp, "GEYSER_GRPC_TOKEN", "")
    monkeypatch.setattr(yp, "GEYSER_USE_TLS", False)
    monkeypatch.setattr(yp, "GEYSER_PRICE_TIMEOUT_SEC", 1.0)


@pytest.fixture
def shared_service(monkeypatch):
    """
    Mock the shared ShyftPricingService singleton used for the pre-subscribe
    migration check, SOL/USD conversion, and the PumpSwap fallback.
    Defaults to "not migrated" / sol_usd=200.0 so existing bonding-curve-path
    tests don't need to know about the migration pre-check at all; tests that
    care can override svc.is_bonding_curve_migrated / svc.get_pumpswap_pool_price.
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
    assert result["source"] == "yellowstone_grpc_accountSubscribe"
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
    """Pre-check catches an already-migrated curve without wasting the subscribe timeout."""
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

    fetch.assert_not_called()  # never opened a subscription for an already-dead curve
    shared_service.get_pumpswap_pool_price.assert_called_once_with(PUMP_MINT)
    assert result["source"] == "shyft_rpc_pumpswap_pool"
    assert result["usdPrice"] == pytest.approx(0.003)


def test_migrates_during_subscription_falls_back_to_pumpswap(shared_service):
    """complete=true arrives mid-subscription (pre-check said not-yet-migrated) -> fall back, don't raise."""
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

    shared_service.get_pumpswap_pool_price.assert_called_once_with(PUMP_MINT)
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


def test_stream_ends_without_account_update_raises():
    """gRPC call succeeds but only sends non-account updates (e.g. pings)."""
    fake_call = MagicMock()
    fake_call.__iter__.return_value = iter([geyser_pb2.SubscribeUpdate()])  # ping-shaped, no account

    with patch.object(yp, "_build_channel") as build_channel, patch(
        "yellowstone_pricing.geyser_pb2_grpc.GeyserStub"
    ) as stub_cls:
        channel = MagicMock()
        build_channel.return_value = channel
        stub = MagicMock()
        stub.Subscribe.return_value = fake_call
        stub_cls.return_value = stub

        with pytest.raises(yp.YellowstonePricingError):
            yp._fetch_bonding_curve_account("SomePda111111111111111111111111", 1.0)

        fake_call.cancel.assert_called_once()
        channel.close.assert_called_once()


def test_grpc_unavailable_raises_yellowstone_error():
    """Simulates Yellowstone being unavailable/timing out."""

    class _FakeRpcError(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "connection refused"

    def _raise(*_args, **_kwargs):
        raise _FakeRpcError()

    with patch.object(yp, "_build_channel") as build_channel, patch(
        "yellowstone_pricing.geyser_pb2_grpc.GeyserStub"
    ) as stub_cls:
        channel = MagicMock()
        build_channel.return_value = channel
        stub = MagicMock()
        stub.Subscribe.side_effect = _raise
        stub_cls.return_value = stub

        with pytest.raises(yp.YellowstonePricingError, match="UNAVAILABLE"):
            yp._fetch_bonding_curve_account("SomePda111111111111111111111111", 1.0)

        channel.close.assert_called_once()


def test_fetch_returns_first_account_update_and_cancels():
    """End-to-end (mocked transport) wiring: request -> stub.Subscribe -> first account payload."""
    raw = _bonding_raw()
    fake_call = MagicMock()
    fake_call.__iter__.return_value = iter([_account_update(raw)])

    with patch.object(yp, "_build_channel") as build_channel, patch(
        "yellowstone_pricing.geyser_pb2_grpc.GeyserStub"
    ) as stub_cls:
        channel = MagicMock()
        build_channel.return_value = channel
        stub = MagicMock()
        stub.Subscribe.return_value = fake_call
        stub_cls.return_value = stub

        data = yp._fetch_bonding_curve_account("SomePda111111111111111111111111", 1.0)

    assert data == raw
    fake_call.cancel.assert_called_once()
    channel.close.assert_called_once()
    # timeout + metadata were passed through to the streaming call
    _, kwargs = stub.Subscribe.call_args
    assert kwargs["timeout"] == 1.0
