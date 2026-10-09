"""
Regression tests for the sniper take-profit bug.

Root cause: evaluate_autosnipe_sell_amount returned "skip" whenever
profit_multiplier * 100 >= drop_until_profit. Every live trade uses
drop_until_profit=99, so any price >= 0.99x the buy price skipped all
take-profit and trailing-stop rules; only the stop loss (checked first) ever
sold. Config values below are the real values from the client's trade table.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from solders.keypair import Keypair

import autosnipe_sell_script as sell_script
from autosnipe_sell_logic import evaluate_autosnipe_sell


def _live_config_trade(**kwargs):
    """A Trade carrying the exact sell config every client trade has."""
    base = dict(
        id=1,
        token_address="Mint1111111111111111111111111111111111pump",
        to_pubkey="WalletPubkey",
        purchased_token_amount=1000.0,
        initial_price=1.0,
        executed=False,
        autosnipe_sell_slippage=100,
        default_gas_fee=0.01,
        drop_cutoff=30,
        drop_cutoff_enabled=True,
        drop_until_profit=99,
        drop_after_100=50,
        drop_after_100_enabled=True,
        drop_after_400=30,
        drop_after_400_enabled=True,
        sell_at_100=10,
        sell_at_200=10,
        sell_at_400=10,
        sell_at_1000=10,
        sell_at_1500=10,
        sell_at_2500=10,
        sell_at_4000=10,
        sell_at_10000=10,
        last_tp_tier=0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


# --- Decision logic -------------------------------------------------------

@pytest.mark.parametrize("price", [1.0, 1.5, 1.99])
def test_below_100_percent_profit_does_not_sell(price):
    decision = evaluate_autosnipe_sell(_live_config_trade(), price, {})
    assert decision.amount == 0
    assert decision.message is None


def test_exactly_100_percent_profit_sells_configured_pct():
    decision = evaluate_autosnipe_sell(_live_config_trade(), 2.0, {})
    assert decision.amount == pytest.approx(100.0)  # 10% of 1000
    assert decision.message == "Auto-Sell 10% at 100% Profit"
    assert decision.take_profit_tier == 2.0


def test_drop_until_profit_99_no_longer_blocks_take_profit():
    """The exact live config that produced "Profit reached limit of 99.0%"."""
    trade = _live_config_trade(drop_until_profit=99)
    decision = evaluate_autosnipe_sell(trade, 2.33, {})
    assert decision.amount > 0
    assert "Profit reached limit" not in (decision.message or "")


@pytest.mark.parametrize(
    "name, initial, current, expected_label, expected_tier",
    [
        # Real open trades priced live from the client's trade table — each was
        # returning "Profit reached limit of 99.0%, skipping further sells."
        ("KIRKDAY #322", 4.867203759355247e-06, 4.867203759355247e-06 * 2.330, "100%", 2.0),
        ("FortniteOG #87", 1.0775447067809357e-05, 1.0775447067809357e-05 * 3.713, "200%", 3.0),
        ("Sicey #240", 5.527842308173664e-06, 5.527842308173664e-06 * 6.945, "400%", 5.0),
        ("kumo #311", 7.518609214447407e-06, 7.518609214447407e-06 * 9.094, "400%", 5.0),
    ],
)
def test_real_trades_above_100_percent_now_sell(name, initial, current, expected_label, expected_tier):
    trade = _live_config_trade(initial_price=initial, purchased_token_amount=20000.0)
    decision = evaluate_autosnipe_sell(trade, current, {})
    assert decision.amount == pytest.approx(2000.0), name
    assert decision.message == f"Auto-Sell 10% at {expected_label} Profit", name
    assert decision.take_profit_tier == expected_tier, name


def test_stop_loss_unchanged_with_live_config():
    decision = evaluate_autosnipe_sell(_live_config_trade(), 0.69, {})
    assert decision.amount == 1000.0
    assert decision.message == "Auto-Sell All: Drops below 30%"
    assert decision.take_profit_tier is None


@pytest.mark.parametrize("price", [0.71, 0.93, 0.99])
def test_small_loss_above_stop_loss_does_not_sell(price):
    decision = evaluate_autosnipe_sell(_live_config_trade(), price, {})
    assert decision.amount == 0


def test_tier_already_sold_is_not_sold_again():
    trade = _live_config_trade(last_tp_tier=2.0)
    decision = evaluate_autosnipe_sell(trade, 2.4, {})
    assert decision.amount == 0


def test_next_tier_still_fires_after_lower_tier_sold():
    trade = _live_config_trade(last_tp_tier=2.0)
    decision = evaluate_autosnipe_sell(trade, 3.2, {})
    assert decision.message == "Auto-Sell 10% at 200% Profit"
    assert decision.take_profit_tier == 3.0


def test_lower_tier_not_resold_after_price_falls_back():
    trade = _live_config_trade(last_tp_tier=5.0)
    decision = evaluate_autosnipe_sell(trade, 2.5, {1: 2.6})
    assert decision.amount == 0


def test_zero_pct_tier_falls_through_to_next_lower_tier():
    trade = _live_config_trade(sell_at_400=0)
    decision = evaluate_autosnipe_sell(trade, 5.5, {})
    assert decision.message == "Auto-Sell 10% at 200% Profit"
    assert decision.take_profit_tier == 3.0


def test_trailing_stop_after_100_percent_reachable_with_live_config():
    # Peak 5.0x, now 2.4x: 52% off the peak >= drop_after_100 (50%).
    decision = evaluate_autosnipe_sell(_live_config_trade(), 2.4, {1: 5.0})
    assert decision.amount == 1000.0
    assert "after 100% profit" in decision.message
    assert decision.take_profit_tier is None


# --- Sell loop (scheduler job) --------------------------------------------

class _FakeVersionedTx:
    wallet_pubkey = None

    def __init__(self, message=None, signers=None):
        self.message = message
        self.signatures = signers or []

    @classmethod
    def from_bytes(cls, _raw):
        return cls(SimpleNamespace(account_keys=[cls.wallet_pubkey]), [None])


def _response(status, payload):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


@pytest.fixture
def harness(tmp_path):
    """Captures the scheduler job and stubs every external dependency."""
    keypair = Keypair()
    key_file = tmp_path / "wallet.key"
    key_file.write_bytes(bytes(keypair))
    _FakeVersionedTx.wallet_pubkey = keypair.pubkey()

    h = SimpleNamespace(trades=[], prices={}, jobs=[], quote_status={}, send_error=None)

    class _Scheduler:
        def __init__(self, *a, **k):
            pass

        def add_job(self, func, *a, **k):
            h.jobs.append(func)

        def start(self):
            pass

    trade_model = MagicMock()
    trade_model.query.filter_by.return_value.order_by.return_value.all.side_effect = (
        lambda: [t for t in h.trades if not t.executed]
    )
    wallet_model = MagicMock()
    wallet_model.query.filter_by.return_value.first.return_value = SimpleNamespace(
        public_key="WalletPubkey", private_key=str(key_file)
    )

    def _quote(url, params=None, **kwargs):
        status = h.quote_status.get(params["inputMint"], 200)
        if status != 200:
            return _response(status, {"error": "No routes found", "errorCode": "NO_ROUTES_FOUND"})
        return _response(200, {"inAmount": str(params["amount"])})

    requests_mock = MagicMock()
    requests_mock.get.side_effect = _quote
    requests_mock.post.return_value = _response(
        200, {"swapTransaction": base64.b64encode(b"tx").decode()}
    )

    def _send(_tx):
        if h.send_error:
            raise RuntimeError(h.send_error)
        h.sent += 1
        return SimpleNamespace(value=f"sig{h.sent}")

    h.sent = 0
    solana_mock = MagicMock()
    solana_mock.send_transaction.side_effect = _send

    def _price(mint):
        price = h.prices[mint]
        if isinstance(price, Exception):
            raise price
        return {"usdPrice": price}

    h.history = MagicMock()
    h.logger = MagicMock()
    sell_script.price_tracking.clear()

    with patch.object(sell_script, "BackgroundScheduler", _Scheduler), \
         patch.object(sell_script, "atexit", MagicMock()), \
         patch.object(sell_script, "Trade", trade_model), \
         patch.object(sell_script, "Wallet", wallet_model), \
         patch.object(sell_script, "TradeHistory", h.history), \
         patch.object(sell_script, "db", MagicMock()), \
         patch.object(sell_script, "requests", requests_mock), \
         patch.object(sell_script, "solana_client", solana_mock), \
         patch.object(sell_script, "VersionedTransaction", _FakeVersionedTx), \
         patch.object(sell_script, "get_token_metadata", return_value=None), \
         patch.object(sell_script, "get_sniper_sell_price", side_effect=_price), \
         patch.object(sell_script, "logger", h.logger):
        sell_script.auto_snipe_auto_sell_schedular(Flask("test"))
        h.run_cycle = h.jobs[0]
        yield h


def _logged(h, level):
    return " ".join(str(c.args[0]) for c in getattr(h.logger, level).call_args_list)


def test_loop_take_profit_and_stop_loss_in_same_cycle(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    sl = _live_config_trade(id=3, token_address="MintSL")
    harness.trades = [tp, sl]
    harness.prices = {"MintTP": 2.33, "MintSL": 0.5}

    harness.run_cycle()

    assert harness.sent == 2
    assert tp.purchased_token_amount == pytest.approx(900.0)
    assert tp.last_tp_tier == 2.0
    assert tp.executed is False
    assert sl.purchased_token_amount == 0
    assert sl.executed is True


def test_loop_does_not_resell_same_tier_next_cycle(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    harness.trades = [tp]
    harness.prices = {"MintTP": 2.33}

    harness.run_cycle()
    harness.run_cycle()
    harness.run_cycle()

    assert harness.sent == 1
    assert tp.purchased_token_amount == pytest.approx(900.0)


def test_loop_jupiter_quote_failure_keeps_trade_retryable(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    other = _live_config_trade(id=3, token_address="MintOther")
    harness.trades = [tp, other]
    harness.prices = {"MintTP": 2.33, "MintOther": 0.5}
    harness.quote_status = {"MintTP": 400}

    harness.run_cycle()

    assert tp.purchased_token_amount == 1000.0
    assert tp.last_tp_tier == 0
    assert "trade 2 (MintTP)" in _logged(harness, "error")
    assert other.executed is True  # failure didn't stop the rest of the cycle

    harness.quote_status = {}
    harness.run_cycle()
    assert tp.last_tp_tier == 2.0
    assert tp.purchased_token_amount == pytest.approx(900.0)


def test_loop_failed_send_does_not_record_sale(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    harness.trades = [tp]
    harness.prices = {"MintTP": 2.33}
    harness.send_error = "Blockhash not found"

    harness.run_cycle()

    assert tp.purchased_token_amount == 1000.0
    assert tp.last_tp_tier == 0
    harness.history.assert_not_called()
    assert "trade 2 (MintTP)" in _logged(harness, "error")


def test_loop_price_fetch_failure_skips_only_that_trade(harness):
    bad = _live_config_trade(id=2, token_address="MintBad")
    tp = _live_config_trade(id=3, token_address="MintTP")
    harness.trades = [bad, tp]
    harness.prices = {"MintBad": RuntimeError("Shyft 429"), "MintTP": 2.0}

    harness.run_cycle()

    assert bad.purchased_token_amount == 1000.0
    assert tp.last_tp_tier == 2.0


def test_loop_missing_key_file_does_not_abort_cycle(harness, tmp_path):
    """Regression: a missing key file used to `return` out of the whole cycle."""
    first = _live_config_trade(id=5, token_address="MintMissingKey")
    second = _live_config_trade(id=4, token_address="MintTP")
    harness.trades = [first, second]
    harness.prices = {"MintMissingKey": 2.33, "MintTP": 2.33}

    wallet_ok = sell_script.Wallet.query.filter_by.return_value.first.return_value
    wallet_missing = SimpleNamespace(public_key="Gone", private_key=str(tmp_path / "missing.key"))
    sell_script.Wallet.query.filter_by.return_value.first.side_effect = [wallet_missing, wallet_ok]

    harness.run_cycle()

    assert first.purchased_token_amount == 1000.0
    assert second.last_tp_tier == 2.0
    assert "Private key file missing" in _logged(harness, "error")
