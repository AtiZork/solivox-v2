"""Unit tests for yellowstone_cache (mocked gRPC transport - no live endpoint)."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from solders.pubkey import Pubkey

import yellowstone_cache as yc
from yellowstone_grpc.generated import geyser_pb2

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Every test starts from an empty cache / disconnected / not-started state."""
    monkeypatch.setattr(yc, "_curve_cache", {})
    monkeypatch.setattr(yc, "_pool_cache_by_mint", {})
    monkeypatch.setattr(yc, "_connected", False)
    monkeypatch.setattr(yc, "_started", False)
    monkeypatch.setattr(yc, "_cache_thread", None)
    yc._stop_event.clear()
    monkeypatch.setattr(yc, "USE_GEYSER", True)
    monkeypatch.setattr(yc, "GEYSER_GRPC_URL", "127.0.0.1:10000")
    yield
    yc._stop_event.set()  # make sure nothing left running tries to proceed


def _curve_update(pubkey_bytes: bytes, data: bytes) -> geyser_pb2.SubscribeUpdate:
    update = geyser_pb2.SubscribeUpdate()
    update.filters.append("pump_curves")
    update.account.account.pubkey = pubkey_bytes
    update.account.account.data = data
    update.account.slot = 1
    return update


def _pool_update(pubkey_bytes: bytes, data: bytes) -> geyser_pb2.SubscribeUpdate:
    update = geyser_pb2.SubscribeUpdate()
    update.filters.append("pumpswap_pools")
    update.account.account.pubkey = pubkey_bytes
    update.account.account.data = data
    update.account.slot = 1
    return update


def _pumpswap_pool_raw(base_mint: str, quote_mint: str, base_vault: str, quote_vault: str,
                        virtual_quote_reserves: int = 1_000_000_000) -> bytes:
    raw = bytearray(301)
    raw[43:75] = bytes(Pubkey.from_string(base_mint))
    raw[75:107] = bytes(Pubkey.from_string(quote_mint))
    raw[139:171] = bytes(Pubkey.from_string(base_vault))
    raw[171:203] = bytes(Pubkey.from_string(quote_vault))
    raw[245:261] = int(virtual_quote_reserves).to_bytes(16, "little", signed=True)
    return bytes(raw)


# --- subscription request shape ---------------------------------------------

def test_subscribe_request_filters_by_owner_and_datasize_only():
    """No unverified discriminator bytes - owner + datasize only, matching the
    plan's deliberate design choice."""
    req = yc._subscribe_request()
    assert list(req.accounts["pump_curves"].owner) == [yc.PUMP_FUN_PROGRAM_ID_STR]
    assert list(req.accounts["pump_curves"].filters) == []  # no memcmp/discriminator filter

    pools = req.accounts["pumpswap_pools"]
    assert list(pools.owner) == [str(yc.PUMPSWAP_PROGRAM_ID)]
    assert len(pools.filters) == 1
    assert pools.filters[0].datasize == yc._PUMPSWAP_POOL_DATA_SIZE
    assert req.commitment == geyser_pb2.CommitmentLevel.PROCESSED


# --- update routing + parsing -------------------------------------------------

def test_curve_update_is_cached_under_its_own_pubkey():
    pda = Pubkey.from_string("3itWj2s5bkmTFGoJHu13MaWpvkdB1fkqqMDnw8DPpump")
    data = b"\x01" * 64
    yc._handle_update(_curve_update(bytes(pda), data))

    assert yc.get_cached_curve_raw(str(pda)) == data


def test_pool_update_is_parsed_and_indexed_by_mint():
    base_vault = str(Pubkey.from_bytes(bytes([7]) * 32))
    quote_vault = str(Pubkey.from_bytes(bytes([9]) * 32))
    quote_mint = "So11111111111111111111111111111111111111112"
    pool_pubkey = Pubkey.from_bytes(bytes([5]) * 32)
    raw = _pumpswap_pool_raw(
        base_mint=PUMP_MINT, quote_mint=quote_mint,
        base_vault=base_vault, quote_vault=quote_vault,
        virtual_quote_reserves=1_000_000_000,
    )

    yc._handle_update(_pool_update(bytes(pool_pubkey), raw))

    metadata = yc.get_cached_pool_metadata(PUMP_MINT)
    assert metadata == {
        "pool_pubkey": str(pool_pubkey),
        "base_mint": PUMP_MINT,
        "quote_mint": quote_mint,
        "base_vault": base_vault,
        "quote_vault": quote_vault,
        "virtual_quote_reserves": 1_000_000_000,
    }


def test_get_cached_pool_metadata_returns_a_copy_not_the_live_dict():
    yc.set_cached_curve_raw("ignored-for-this-test", b"x")  # ensure module usable
    with yc._pool_cache_lock:
        yc._pool_cache_by_mint[PUMP_MINT] = {"pool_pubkey": "P1", "base_mint": PUMP_MINT}
    metadata = yc.get_cached_pool_metadata(PUMP_MINT)
    metadata["base_mint"] = "tampered"
    assert yc.get_cached_pool_metadata(PUMP_MINT)["base_mint"] == PUMP_MINT


def test_malformed_pool_update_is_logged_and_skipped_not_raised():
    """A too-short/garbage account under the pool filter must never crash the
    stream-reading loop - one bad account shouldn't take down pricing for
    every other open position."""
    pool_pubkey = Pubkey.from_bytes(bytes([5]) * 32)
    yc._handle_update(_pool_update(bytes(pool_pubkey), b"\x00" * 10))  # far too short
    assert yc.get_cached_pool_metadata(PUMP_MINT) is None


def test_ping_only_update_is_ignored():
    update = geyser_pb2.SubscribeUpdate()  # no account field set
    yc._handle_update(update)  # must not raise
    assert yc.cache_status()["curves_cached"] == 0


# --- public API round-trips ---------------------------------------------------

def test_set_and_get_cached_curve_raw_round_trip():
    yc.set_cached_curve_raw("SomePda111111111111111111111111", b"raw-bytes")
    assert yc.get_cached_curve_raw("SomePda111111111111111111111111") == b"raw-bytes"
    assert yc.get_cached_curve_raw("NeverSeen1111111111111111111111") is None


def test_cache_status_reports_counts():
    yc.set_cached_curve_raw("Pda1", b"x")
    yc.set_cached_curve_raw("Pda2", b"y")
    status = yc.cache_status()
    assert status["curves_cached"] == 2
    assert status["pools_cached"] == 0
    assert status["connected"] is False


# --- start/stop lifecycle ------------------------------------------------------

def test_start_is_a_noop_when_yellowstone_disabled(monkeypatch):
    monkeypatch.setattr(yc, "USE_GEYSER", False)
    assert yc.start_yellowstone_cache() is False
    assert yc._cache_thread is None


def test_start_spawns_exactly_one_thread_even_if_called_twice(monkeypatch):
    created_threads = []

    class _FakeThread:
        def __init__(self, target, name, daemon):
            self.target, self.name, self.daemon = target, name, daemon
            self._alive = True
            created_threads.append(self)

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    assert yc.start_yellowstone_cache() is True
    assert yc.start_yellowstone_cache() is True  # second call: no new thread
    assert len(created_threads) == 1


def test_stop_clears_started_flag_and_sets_stop_event():
    yc._started = True
    yc.stop_yellowstone_cache()
    assert yc._started is False
    assert yc._stop_event.is_set()


# --- reconnect loop: single-pass failure handling ------------------------------

def test_run_cache_thread_marks_disconnected_and_backs_off_on_stream_error():
    """One connect attempt that raises -> loop logs + waits before the next
    attempt, without crashing the thread. Drives exactly one iteration by
    having the backoff wait itself set the stop event."""
    with patch.object(yc, "_channel", side_effect=RuntimeError("connection refused")):
        def _stop_after_wait(timeout=None):
            yc._stop_event.set()
            return True

        with patch.object(yc._stop_event, "wait", side_effect=_stop_after_wait):
            yc._run_cache_thread()

    assert yc.is_cache_connected() is False
