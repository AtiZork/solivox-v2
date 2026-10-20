"""
reconcile_pending_sells: a sent sell transaction only means the RPC node
accepted it for broadcast, not that it landed on-chain. Before this job
existed, a sell that failed after send_transaction (expired blockhash,
liquidity moved, node dropped it) still left the trade marked executed with
purchased_token_amount already reduced, so no rule ever reconsidered the
tokens the wallet still actually held.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from solders.transaction_status import TransactionConfirmationStatus

import autosnipe_sell_script as sell_script


def _history(**kwargs):
    base = dict(
        id=1,
        trade_id=1,
        token_address="MintX",
        trade_type="SELL",
        trade_kind="AutoSnipe",
        amount=100.0,
        execution_price=1.0,
        tx_id="sig1",
        status="sent",
        timestamp=datetime.utcnow(),
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _trade(**kwargs):
    base = dict(id=1, token_address="MintX", purchased_token_amount=0.0, executed=True)
    base.update(kwargs)
    return SimpleNamespace(**base)


def _status(err=None, confirmation_status=TransactionConfirmationStatus.Confirmed):
    return SimpleNamespace(err=err, confirmation_status=confirmation_status)


@pytest.fixture
def harness():
    h = SimpleNamespace(history_rows=[], trades={}, statuses={})

    history_model = MagicMock()
    history_model.query.filter_by.side_effect = lambda **kw: SimpleNamespace(
        all=lambda: [
            r for r in h.history_rows
            if r.status == kw.get("status") and r.trade_type == kw.get("trade_type")
        ]
    )
    trade_model = MagicMock()
    trade_model.query.get.side_effect = lambda tid: h.trades.get(tid)

    def _get_statuses(sigs, **kwargs):
        return SimpleNamespace(value=[h.statuses.get(sigs[0])])

    solana_mock = MagicMock()
    solana_mock.get_signature_statuses.side_effect = _get_statuses

    h.db = MagicMock()
    h.app = Flask("test")
    h.logger = MagicMock()
    sell_script._retry_after.clear()
    with patch.object(sell_script, "TradeHistory", history_model), \
         patch.object(sell_script, "Trade", trade_model), \
         patch.object(sell_script, "db", h.db), \
         patch.object(sell_script, "solana_client", solana_mock), \
         patch.object(sell_script, "Signature", SimpleNamespace(from_string=lambda s: s)), \
         patch.object(sell_script, "logger", h.logger), \
         patch.object(sell_script, "SNIPER_SELL_RECONCILE_TIMEOUT_SEC", 90.0):
        yield h


def test_no_pending_sells_is_a_noop(harness):
    sell_script.reconcile_pending_sells(harness.app)
    harness.db.session.commit.assert_not_called()


def test_confirmed_sell_marked_confirmed_trade_left_alone(harness):
    history = _history(tx_id="sig1")
    trade = _trade(id=1, purchased_token_amount=0.0, executed=True)
    harness.history_rows = [history]
    harness.trades = {1: trade}
    harness.statuses = {"sig1": _status(confirmation_status=TransactionConfirmationStatus.Confirmed)}

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "confirmed"
    assert trade.purchased_token_amount == 0.0
    assert trade.executed is True


def test_signature_status_lookup_uses_search_transaction_history(harness):
    """Regression guard: without search_transaction_history=True, the RPC
    node's short-lived recent-status cache can evict an older signature and
    return None even for a transaction that actually landed, eventually
    "restoring" a sell that actually succeeded (a phantom open position).
    Matches the equivalent guard on the buy-side reconcile."""
    history = _history(tx_id="sig1")
    trade = _trade(id=1)
    harness.history_rows = [history]
    harness.trades = {1: trade}
    harness.statuses = {"sig1": _status()}

    with patch.object(
        sell_script.solana_client, "get_signature_statuses",
        wraps=lambda sigs, **kw: SimpleNamespace(value=[harness.statuses.get(sigs[0])]),
    ) as get_statuses:
        sell_script.reconcile_pending_sells(harness.app)

    assert get_statuses.call_args.kwargs.get("search_transaction_history") is True


def test_finalized_also_counts_as_confirmed(harness):
    history = _history(tx_id="sig1")
    harness.history_rows = [history]
    harness.trades = {1: _trade()}
    harness.statuses = {"sig1": _status(confirmation_status=TransactionConfirmationStatus.Finalized)}

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "confirmed"


def test_failed_onchain_sell_restores_position_and_reopens_trade(harness):
    history = _history(tx_id="sig1", trade_id=5, amount=250.0)
    trade = _trade(id=5, purchased_token_amount=0.0, executed=True)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status(err={"InstructionError": [0, "Custom"]})}

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "failed"
    assert trade.purchased_token_amount == pytest.approx(250.0)
    assert trade.executed is False


def test_restore_pops_retry_cooldown_so_next_cycle_retries_immediately(harness):
    history = _history(tx_id="sig1", trade_id=5, amount=10.0)
    trade = _trade(id=5, purchased_token_amount=0.0, executed=True)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status(err="any")}
    sell_script._retry_after[5] = sell_script.time.monotonic() + 999

    sell_script.reconcile_pending_sells(harness.app)

    assert 5 not in sell_script._retry_after


def test_unresolved_signature_within_timeout_stays_pending(harness):
    history = _history(tx_id="sig1", timestamp=datetime.utcnow())
    trade = _trade()
    harness.history_rows = [history]
    harness.trades = {1: trade}
    harness.statuses = {}  # get_signature_statuses returns None: not seen yet

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "sent"
    assert trade.executed is True  # untouched


def test_unresolved_signature_past_timeout_is_treated_as_failed(harness):
    history = _history(tx_id="sig1", trade_id=5, amount=42.0, timestamp=datetime.utcnow() - timedelta(seconds=200))
    trade = _trade(id=5, purchased_token_amount=0.0, executed=True)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {}

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "failed_timeout"
    assert trade.purchased_token_amount == pytest.approx(42.0)
    assert trade.executed is False


def test_processed_but_not_yet_confirmed_within_window_stays_pending(harness):
    history = _history(tx_id="sig1", timestamp=datetime.utcnow())
    harness.history_rows = [history]
    harness.trades = {1: _trade()}
    harness.statuses = {"sig1": _status(confirmation_status=TransactionConfirmationStatus.Processed)}

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "sent"


def test_processed_but_never_confirms_past_timeout_restores(harness):
    history = _history(
        tx_id="sig1", trade_id=5, amount=30.0, timestamp=datetime.utcnow() - timedelta(seconds=200)
    )
    trade = _trade(id=5, purchased_token_amount=0.0, executed=True)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status(confirmation_status=TransactionConfirmationStatus.Processed)}

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "failed_timeout"
    assert trade.purchased_token_amount == pytest.approx(30.0)


def test_missing_trade_row_still_resolves_history_status(harness):
    """Trade was deleted/cascaded; the history row must still get a final status."""
    history = _history(tx_id="sig1", trade_id=999)
    harness.history_rows = [history]
    harness.trades = {}  # trade 999 not found
    harness.statuses = {"sig1": _status(err="any")}

    sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "failed"


def test_status_lookup_failure_leaves_row_pending_for_next_run(harness):
    history = _history(tx_id="sig1")
    trade = _trade()
    harness.history_rows = [history]
    harness.trades = {1: trade}

    def _boom(sigs, **kwargs):
        raise RuntimeError("RPC down")

    with patch.object(sell_script.solana_client, "get_signature_statuses", side_effect=_boom):
        sell_script.reconcile_pending_sells(harness.app)

    assert history.status == "sent"
    assert trade.executed is True


def test_multiple_pending_sells_each_resolved_independently(harness):
    confirmed = _history(id=1, tx_id="sigA", trade_id=1, amount=10.0)
    failed = _history(id=2, tx_id="sigB", trade_id=2, amount=20.0)
    harness.history_rows = [confirmed, failed]
    harness.trades = {
        1: _trade(id=1, purchased_token_amount=0.0, executed=True),
        2: _trade(id=2, purchased_token_amount=0.0, executed=True),
    }
    harness.statuses = {
        "sigA": _status(confirmation_status=TransactionConfirmationStatus.Confirmed),
        "sigB": _status(err="any"),
    }

    sell_script.reconcile_pending_sells(harness.app)

    assert confirmed.status == "confirmed"
    assert harness.trades[1].executed is True
    assert failed.status == "failed"
    assert harness.trades[2].executed is False
    assert harness.trades[2].purchased_token_amount == pytest.approx(20.0)


def test_buy_rows_awaiting_their_own_reconciliation_are_never_touched(harness):
    """Regression: a BUY row sitting at status="sent" (awaiting
    autosnipe_buy_new_token.py's own reconcile_pending_buys) must never be
    picked up here - this job has no concept of an entry price, so it would
    mark the row "confirmed" without ever computing one, permanently
    stranding Trade.initial_price at its placeholder."""
    buy_row = _history(trade_id=5, tx_id="sigBuy", trade_type="BUY")
    harness.history_rows = [buy_row]
    harness.trades = {5: _trade(id=5)}
    harness.statuses = {"sigBuy": _status(confirmation_status=TransactionConfirmationStatus.Confirmed)}

    sell_script.reconcile_pending_sells(harness.app)

    assert buy_row.status == "sent"  # never touched - not a SELL row


def test_scheduler_wires_reconcile_job_without_disturbing_sell_cycle_job():
    """h.jobs[0] (the sell-decision cycle) must stay the first job added."""
    jobs = []

    class _Scheduler:
        def __init__(self, *a, **k):
            pass

        def add_job(self, func, *a, **k):
            jobs.append(func)

        def start(self):
            pass

    with patch.object(sell_script, "BackgroundScheduler", _Scheduler), \
         patch.object(sell_script, "SellStream", MagicMock()), \
         patch.object(sell_script, "SNIPER_SELL_STREAM", False), \
         patch.object(sell_script, "atexit", MagicMock()):
        sell_script.auto_snipe_auto_sell_schedular(Flask("test"))

    assert len(jobs) == 3  # cycle, reconcile, inactivity sweep
    assert jobs[0].__name__ == "auto_snipe_sell"
