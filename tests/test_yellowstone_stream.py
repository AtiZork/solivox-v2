"""
Unit tests for YellowstonePriceStream (the persistent subscription + cache)
and the module-level get_yellowstone_price_cached()/start/stop helpers.

No live gRPC endpoint involved: _build_channel/geyser_pb2_grpc.GeyserStub
are never exercised here directly except where explicitly noted - these
tests drive the stream's internal methods (seeding, sync, update handling)
directly, the same way the background threads would call them.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import yellowstone_pricing as yp
from yellowstone_grpc.generated import geyser_pb2

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"
OTHER_MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
BONDING_CURVE = "4nMhJXcDEJcAgkKsy4xPwMHdPqPPMnqMwE4Drse4u6P1"


def _bonding_raw(vt: int = 1_000_000_000_000, vs: int = 30_000_000_000, complete: bool = False) -> bytes:
    raw = bytearray(64)
    raw[8:16] = int(vt).to_bytes(8, "little")
    raw[16:24] = int(vs).to_bytes(8, "little")
    raw[48] = 1 if complete else 0
    return bytes(raw)


def _account_update(pubkey: bytes, data: bytes) -> geyser_pb2.SubscribeUpdate:
    update = geyser_pb2.SubscribeUpdate()
    update.account.account.pubkey = pubkey
    update.account.account.data = data
    update.account.slot = 1
    return update


def _bonding_curve_price(mint: str, usd: float = 1.0) -> dict:
    return {
        "mint": mint,
        "bonding_curve": BONDING_CURVE,
        "price_in_sol": 0.01,
        "usd_price": usd,
        "usdPrice": usd,
        "sol_price_usd": 200.0,
        "source": "shyft_rpc_pumpfun_bonding_curve",
    }


def _pumpswap_price(mint: str, usd: float = 2.0) -> dict:
    return {
        "mint": mint,
        "usd_price": usd,
        "usdPrice": usd,
        "source": "shyft_rpc_pumpswap_pool",
    }


@pytest.fixture
def stream():
    s = yp.YellowstonePriceStream(get_open_mints=lambda: set(), sync_interval=5.0)
    return s


@pytest.fixture(autouse=True)
def _mock_bonding_curve_pda(monkeypatch):
    monkeypatch.setattr(
        yp.ShyftPricingService, "bonding_curve_pda", staticmethod(lambda mint: BONDING_CURVE)
    )


@pytest.fixture(autouse=True)
def _clear_singleton():
    yp._price_stream = None
    yield
    yp._price_stream = None


# --- _seed_and_track / ensure_tracked ---------------------------------------


def test_seed_and_track_caches_and_registers_pubkey(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)) as gtp:
        result = stream.ensure_tracked(PUMP_MINT)

    gtp.assert_called_once_with(PUMP_MINT, include_token_details=False)
    assert result["usdPrice"] == 1.0
    assert stream.get_cached_price(PUMP_MINT) == result
    assert stream._tracked[PUMP_MINT] == BONDING_CURVE
    assert stream._pubkey_to_mint[yp._pubkey_bytes(BONDING_CURVE)] == PUMP_MINT


def test_seed_and_track_pushes_filter_when_connected(stream):
    fake_queue = MagicMock()
    stream._current_queue = fake_queue
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)):
        stream.ensure_tracked(PUMP_MINT)

    fake_queue.put.assert_called_once()


def test_seed_and_track_migrated_mint_not_registered(stream):
    with patch.object(yp, "get_token_price", return_value=_pumpswap_price(PUMP_MINT)) as gtp:
        result = stream.ensure_tracked(PUMP_MINT)

    gtp.assert_called_once()
    assert result["source"] == "shyft_rpc_pumpswap_pool"
    assert PUMP_MINT not in stream._tracked
    assert stream.get_cached_price(PUMP_MINT) is None


def test_second_ensure_tracked_call_hits_cache_not_rpc(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)) as gtp:
        stream.ensure_tracked(PUMP_MINT)
        stream.ensure_tracked(PUMP_MINT)

    gtp.assert_called_once()


# --- get_cached_price staleness safety net ----------------------------------


def test_get_cached_price_returns_none_when_stale(stream, monkeypatch):
    monkeypatch.setattr(yp, "GEYSER_PRICE_CACHE_MAX_AGE_SEC", 10.0)
    stream._cache[PUMP_MINT] = _bonding_curve_price(PUMP_MINT)
    stream._cache_at[PUMP_MINT] = yp.time.monotonic() - 999
    assert stream.get_cached_price(PUMP_MINT) is None


def test_get_cached_price_returns_fresh_entry(stream, monkeypatch):
    monkeypatch.setattr(yp, "GEYSER_PRICE_CACHE_MAX_AGE_SEC", 30.0)
    expected = _bonding_curve_price(PUMP_MINT)
    stream._cache[PUMP_MINT] = expected
    stream._cache_at[PUMP_MINT] = yp.time.monotonic()
    assert stream.get_cached_price(PUMP_MINT) == expected


def test_get_cached_price_missing_mint_returns_none(stream):
    assert stream.get_cached_price("unknown") is None


# --- _sync_mints -------------------------------------------------------------


def test_sync_mints_adds_newly_open_positions(stream):
    stream._get_open_mints = lambda: {PUMP_MINT}
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)):
        stream._sync_mints()

    assert PUMP_MINT in stream._tracked


def test_sync_mints_removes_closed_positions(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)):
        stream.ensure_tracked(PUMP_MINT)
    assert PUMP_MINT in stream._tracked

    stream._get_open_mints = lambda: set()  # position closed
    fake_queue = MagicMock()
    stream._current_queue = fake_queue
    stream._sync_mints()

    assert PUMP_MINT not in stream._tracked
    assert PUMP_MINT not in stream._cache
    assert yp._pubkey_bytes(BONDING_CURVE) not in stream._pubkey_to_mint
    fake_queue.put.assert_called()  # filter update pushed for the removal


def test_sync_mints_handles_get_open_mints_failure_gracefully(stream):
    def _boom():
        raise RuntimeError("DB down")

    stream._get_open_mints = _boom
    stream._sync_mints()  # must not raise


# --- _handle_update ----------------------------------------------------------


def test_handle_update_refreshes_cache_for_tracked_pubkey(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT, usd=1.0)):
        stream.ensure_tracked(PUMP_MINT)

    svc = MagicMock()
    svc.get_sol_usd.return_value = 150.0
    with patch.object(yp, "get_shyft_pricing_service", lambda: svc):
        update = _account_update(yp._pubkey_bytes(BONDING_CURVE), _bonding_raw())
        stream._handle_update(update)

    cached = stream.get_cached_price(PUMP_MINT)
    assert cached["source"] == "yellowstone_grpc_accountSubscribe"
    assert cached["sol_price_usd"] == 150.0
    assert cached["usdPrice"] == pytest.approx(cached["price_in_sol"] * 150.0)


def test_handle_update_ignores_unknown_pubkey(stream):
    update = _account_update(b"\x01" * 32, _bonding_raw())
    stream._handle_update(update)  # must not raise, nothing cached
    assert stream._cache == {}


def test_handle_update_ignores_non_account_messages(stream):
    stream._handle_update(geyser_pb2.SubscribeUpdate())  # ping-shaped, no account field


def test_handle_update_zero_price_does_not_overwrite_cache(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT, usd=1.0)):
        stream.ensure_tracked(PUMP_MINT)
    before = stream.get_cached_price(PUMP_MINT)

    update = _account_update(yp._pubkey_bytes(BONDING_CURVE), _bonding_raw(vt=0, vs=0))
    stream._handle_update(update)

    assert stream.get_cached_price(PUMP_MINT) == before


def test_handle_update_migration_evicts_mint_and_pushes_filter(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)):
        stream.ensure_tracked(PUMP_MINT)
    fake_queue = MagicMock()
    stream._current_queue = fake_queue

    update = _account_update(yp._pubkey_bytes(BONDING_CURVE), _bonding_raw(complete=True))
    stream._handle_update(update)

    assert PUMP_MINT not in stream._tracked
    assert stream.get_cached_price(PUMP_MINT) is None
    fake_queue.put.assert_called_once()


# --- _reseed_all_tracked (runs right after (re)connect) ---------------------


def test_reseed_all_tracked_refreshes_every_tracked_mint(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT, usd=1.0)):
        stream.ensure_tracked(PUMP_MINT)

    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT, usd=9.0)) as gtp:
        stream._reseed_all_tracked()

    gtp.assert_called_once_with(PUMP_MINT, include_token_details=False)
    assert stream.get_cached_price(PUMP_MINT)["usdPrice"] == 9.0


def test_reseed_all_tracked_drops_mint_that_migrated_while_disconnected(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)):
        stream.ensure_tracked(PUMP_MINT)

    with patch.object(yp, "get_token_price", return_value=_pumpswap_price(PUMP_MINT)):
        stream._reseed_all_tracked()

    assert PUMP_MINT not in stream._tracked
    assert stream.get_cached_price(PUMP_MINT) is None


def test_reseed_all_tracked_tolerates_rpc_failure(stream):
    with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT)):
        stream.ensure_tracked(PUMP_MINT)

    with patch.object(yp, "get_token_price", side_effect=RuntimeError("rpc down")):
        stream._reseed_all_tracked()  # must not raise

    # stale entry is left in place rather than dropped on a transient failure
    assert PUMP_MINT in stream._tracked


# --- module-level get_yellowstone_price_cached ------------------------------


def test_get_yellowstone_price_cached_disabled_raises(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", False)
    with pytest.raises(yp.YellowstonePricingError):
        yp.get_yellowstone_price_cached(PUMP_MINT)


def test_get_yellowstone_price_cached_missing_mint_raises(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    with pytest.raises(yp.YellowstonePricingError):
        yp.get_yellowstone_price_cached("   ")


def test_get_yellowstone_price_cached_migrated_mint_skips_stream(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    svc = MagicMock()
    svc.is_bonding_curve_migrated.return_value = True
    svc.get_pumpswap_pool_price.return_value = {
        "mint": PUMP_MINT, "usd_price": 3.0, "source": "shyft_rpc_pumpswap_pool",
    }
    fake_stream = MagicMock()
    monkeypatch.setattr(yp, "get_shyft_pricing_service", lambda: svc)
    monkeypatch.setattr(yp, "_price_stream", fake_stream)

    result = yp.get_yellowstone_price_cached(PUMP_MINT)

    assert result["source"] == "shyft_rpc_pumpswap_pool"
    fake_stream.get_cached_price.assert_not_called()
    fake_stream.ensure_tracked.assert_not_called()


def test_get_yellowstone_price_cached_falls_back_to_one_shot_when_stream_not_started(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    svc = MagicMock()
    svc.is_bonding_curve_migrated.return_value = False
    monkeypatch.setattr(yp, "get_shyft_pricing_service", lambda: svc)
    monkeypatch.setattr(yp, "_price_stream", None)

    with patch.object(yp, "get_yellowstone_price", return_value={"usdPrice": 5.0}) as one_shot:
        result = yp.get_yellowstone_price_cached(PUMP_MINT)

    one_shot.assert_called_once_with(PUMP_MINT)
    assert result["usdPrice"] == 5.0


def test_get_yellowstone_price_cached_returns_cache_hit_without_seeding(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    svc = MagicMock()
    svc.is_bonding_curve_migrated.return_value = False
    monkeypatch.setattr(yp, "get_shyft_pricing_service", lambda: svc)

    fake_stream = MagicMock()
    fake_stream.get_cached_price.return_value = {"usdPrice": 7.0}
    monkeypatch.setattr(yp, "_price_stream", fake_stream)

    result = yp.get_yellowstone_price_cached(PUMP_MINT)

    assert result["usdPrice"] == 7.0
    fake_stream.ensure_tracked.assert_not_called()


def test_get_yellowstone_price_cached_seeds_when_no_cache_entry(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    svc = MagicMock()
    svc.is_bonding_curve_migrated.return_value = False
    monkeypatch.setattr(yp, "get_shyft_pricing_service", lambda: svc)

    fake_stream = MagicMock()
    fake_stream.get_cached_price.return_value = None
    fake_stream.ensure_tracked.return_value = {"usdPrice": 8.0}
    monkeypatch.setattr(yp, "_price_stream", fake_stream)

    result = yp.get_yellowstone_price_cached(PUMP_MINT)

    fake_stream.ensure_tracked.assert_called_once_with(PUMP_MINT)
    assert result["usdPrice"] == 8.0


def test_get_yellowstone_price_cached_wraps_seed_failure(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    svc = MagicMock()
    svc.is_bonding_curve_migrated.return_value = False
    monkeypatch.setattr(yp, "get_shyft_pricing_service", lambda: svc)

    fake_stream = MagicMock()
    fake_stream.get_cached_price.return_value = None
    fake_stream.ensure_tracked.side_effect = RuntimeError("rpc down")
    monkeypatch.setattr(yp, "_price_stream", fake_stream)

    with pytest.raises(yp.YellowstonePricingError):
        yp.get_yellowstone_price_cached(PUMP_MINT)


# --- start_yellowstone_stream / stop_yellowstone_stream ---------------------


def test_start_yellowstone_stream_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", False)
    assert yp.start_yellowstone_stream(lambda: set()) is None


def test_start_yellowstone_stream_creates_and_memoizes_singleton(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    fake_stream = MagicMock()
    fake_cls = MagicMock(return_value=fake_stream)
    monkeypatch.setattr(yp, "YellowstonePriceStream", fake_cls)

    first = yp.start_yellowstone_stream(lambda: set())
    second = yp.start_yellowstone_stream(lambda: set())

    fake_stream.start.assert_called_once()
    fake_cls.assert_called_once()  # second call reused the existing instance
    assert first is fake_stream
    assert second is fake_stream


def test_stop_yellowstone_stream_stops_and_clears_singleton(monkeypatch):
    fake_stream = MagicMock()
    monkeypatch.setattr(yp, "_price_stream", fake_stream)

    yp.stop_yellowstone_stream()

    fake_stream.stop.assert_called_once()
    assert yp._price_stream is None


def test_stop_yellowstone_stream_noop_when_not_started():
    yp._price_stream = None
    yp.stop_yellowstone_stream()  # must not raise
