"""
Sniper buy-counting RPC: transaction versions, retries and the throttle.

Live check before the fix (Shyft mainnet, 30 recent Pump.fun txs): 6 of 16
fetched were version 1, and every one of those failed with
UnsupportedTransactionVersion when requested with
max_supported_transaction_version=0 — so those buys were never counted, and
each burned 5 attempts (~4s of sleeps) on the throttled RPC lock.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from solana.rpc.core import RPCException
from solders.rpc import errors
from solders.rpc.responses import GetTransactionResp

import sniper_stream as ss

FIXTURE = Path(__file__).parent / "fixtures_v1_pump_buy_tx.json"
FIXTURE_SIG = "43LM8cNAEFC4Dn2wX1pswLXrPjigik6idY6QPZk2APJG8u6vtpQBBarPJN4UTnxiNXwXERpy9AEGmuCKfxFApSva"
FIXTURE_MINT = "BbwMpDMkXf4VE8p2gBFPnv1sP8kwHmmHyP7ma6yfpump"


@pytest.fixture
def no_sleep():
    with patch.object(ss.time, "sleep") as sleep:
        yield sleep


@pytest.fixture
def stats():
    fresh = ss.StageStats("test", interval_sec=0)
    with patch.object(ss, "rpc_stats", fresh):
        yield fresh


def _unsupported_version_error():
    return RPCException(errors.UnsupportedTransactionVersionMessage(
        "Transaction version (1) is not supported by the requesting client. Please try the request "
        'again with the following configuration parameter: "maxSupportedTransactionVersion": 1'
    ))


def test_get_transaction_requests_version_1(no_sleep, stats):
    client = MagicMock()
    client.get_transaction.return_value = SimpleNamespace(value="tx")
    with patch.object(ss, "solana_client", client), patch.object(ss, "_RPC_MIN_INTERVAL_SEC", 0):
        assert ss._get_transaction_with_retry(FIXTURE_SIG) == "tx"
    assert client.get_transaction.call_args.kwargs["max_supported_transaction_version"] == 1


def test_unsupported_version_is_not_retried(no_sleep, stats):
    client = MagicMock()
    client.get_transaction.side_effect = _unsupported_version_error()
    with patch.object(ss, "solana_client", client), patch.object(ss, "_RPC_MIN_INTERVAL_SEC", 0):
        assert ss._get_transaction_with_retry(FIXTURE_SIG) is None
    assert client.get_transaction.call_count == 1  # previously 5 attempts + ~4s of sleeps
    no_sleep.assert_not_called()
    assert stats.snapshot()["counts"]["err_unsupported_tx_version"] == 1


def test_rate_limit_is_still_retried(no_sleep, stats):
    client = MagicMock()
    client.get_transaction.side_effect = [RuntimeError("429 Too Many Requests"), SimpleNamespace(value="tx")]
    with patch.object(ss, "solana_client", client), patch.object(ss, "_RPC_MIN_INTERVAL_SEC", 0):
        assert ss._get_transaction_with_retry(FIXTURE_SIG) == "tx"
    assert client.get_transaction.call_count == 2
    assert stats.snapshot()["counts"]["err_rate_limited"] == 1


def test_tx_not_indexed_yet_is_retried_then_gives_up(no_sleep, stats):
    client = MagicMock()
    client.get_transaction.return_value = SimpleNamespace(value=None)
    with patch.object(ss, "solana_client", client), patch.object(ss, "_RPC_MIN_INTERVAL_SEC", 0):
        assert ss._get_transaction_with_retry(FIXTURE_SIG, max_attempts=3) is None
    assert client.get_transaction.call_count == 3
    counts = stats.snapshot()["counts"]
    assert counts["tx_not_found_yet"] == 3 and counts["resolve_gave_up"] == 1


def test_real_version_1_buy_tx_resolves_mint_and_sol(stats):
    """A real v1 Pump.fun buy, parsed by solders exactly as the bot receives it."""
    resp = GetTransactionResp.from_json(FIXTURE.read_text())
    assert resp.value.transaction.version == 1
    client = MagicMock()
    client.get_transaction.return_value = resp
    with patch.object(ss, "solana_client", client), patch.object(ss, "_RPC_MIN_INTERVAL_SEC", 0):
        mint = ss._mint_from_tx_value(ss._get_transaction_with_retry(FIXTURE_SIG))
        sol = ss._resolve_buy_amount(FIXTURE_MINT, FIXTURE_SIG)
    assert mint == FIXTURE_MINT
    assert sol == pytest.approx(0.044938377)


def test_throttle_disabled_at_zero_never_takes_lock_or_sleeps(no_sleep):
    with patch.object(ss, "_RPC_MIN_INTERVAL_SEC", 0), patch.object(ss, "_RPC_LOCK", MagicMock()) as lock:
        for _ in range(10):
            ss._rpc_throttle()
    lock.__enter__.assert_not_called()
    no_sleep.assert_not_called()


def test_throttle_spaces_calls_by_interval():
    with patch.object(ss, "_RPC_MIN_INTERVAL_SEC", 0.05), patch.object(ss, "_LAST_RPC_AT", 0.0):
        start = time.monotonic()
        for _ in range(5):
            ss._rpc_throttle()
        elapsed = time.monotonic() - start
    # 4 gaps of 0.05s after the first call; tolerance for Windows' ~15ms clock tick.
    assert elapsed >= 0.15


def test_throttle_interval_from_settings_is_never_negative():
    import importlib
    import settings
    with patch.dict("os.environ", {"SNIPER_RPC_MIN_INTERVAL_SEC": "-1"}):
        try:
            assert importlib.reload(settings).SNIPER_RPC_MIN_INTERVAL_SEC == 0.0
        finally:
            importlib.reload(settings)


def test_submit_logged_records_queue_wait(stats):
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="testpool")
    gate = threading.Event()
    done = threading.Event()
    ss._submit_logged(pool, gate.wait)  # occupies the only worker
    ss._submit_logged(pool, done.set)
    time.sleep(0.1)
    gate.set()
    assert done.wait(2)
    pool.shutdown(wait=True)
    count, _total, peak = stats.snapshot()["durations"]["queue_wait.testpool"]
    assert count == 2
    assert peak >= 90  # the second job waited ~100ms for the worker


def test_buy_count_lag_is_recorded(stats, no_sleep):
    with patch.object(ss, "_resolve_buy_amount", return_value=1.0), \
         patch.object(ss, "_get_sol_usd_price", return_value=150.0), \
         patch.object(ss, "stream_state") as state:
        state.mark_signature_seen.return_value = True
        state.over_candidate_ceiling.return_value = False
        state.get_buy_stats.return_value = {"total_buys": 1}
        ss._process_mint_buy(FIXTURE_MINT, "sig-a", ["Program log: Instruction: Buy"], time.monotonic() - 0.2)
    count, _total, peak = stats.snapshot()["durations"]["buy_ws_to_counted"]
    assert count == 1 and peak >= 190
