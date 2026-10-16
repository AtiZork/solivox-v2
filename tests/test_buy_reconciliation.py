"""
reconcile_pending_buys: fills in Trade.initial_price/purchased_token_amount
from the 0.0 placeholder to the confirmed buy transaction's actual SOL
spent / tokens received - never from a later live price read (a live price
keeps moving, so reading it later would silently substitute "whatever the
price is right now" for "what was actually paid"), never from Shyft or
Geyser - only a direct Pyth SOL/USD read (get_sol_usd_rate) plus the
confirmed transaction's own balance delta.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from solders.pubkey import Pubkey
from solders.transaction_status import TransactionConfirmationStatus

import autosnipe_buy_new_token as buy

MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"
OWNER = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def _balance(mint: str, owner: str, ui_amount):
    return SimpleNamespace(
        mint=Pubkey.from_string(mint),
        owner=Pubkey.from_string(owner),
        ui_token_amount=SimpleNamespace(ui_amount=ui_amount),
    )


def _tx_data(post_balances, pre_balances):
    return SimpleNamespace(
        transaction=SimpleNamespace(
            meta=SimpleNamespace(post_token_balances=post_balances, pre_token_balances=pre_balances)
        )
    )


def _history(**kwargs):
    base = dict(
        id=1, trade_id=1, token_address=MINT, trade_type="BUY", trade_kind="AUTOSNIPE",
        amount=0.5, execution_price=0.0, tx_id="sig1", status="sent", timestamp=datetime.utcnow(),
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _trade(**kwargs):
    base = dict(
        id=1, token_address=MINT, to_pubkey=OWNER, amount=0.5, initial_price=0.0,
        purchased_token_amount=900.0, initial_token_amount=900.0, executed=False, tp_pct_sold=0.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _status(err=None, confirmation_status=TransactionConfirmationStatus.Confirmed):
    return SimpleNamespace(err=err, confirmation_status=confirmation_status)


@pytest.fixture
def harness():
    h = SimpleNamespace(history_rows=[], trades={}, statuses={}, transactions={})

    history_model = MagicMock()
    history_model.query.filter_by.side_effect = lambda **kw: SimpleNamespace(
        all=lambda: [r for r in h.history_rows if r.status == kw.get("status") and r.trade_type == kw.get("trade_type")]
    )
    trade_model = MagicMock()
    trade_model.query.get.side_effect = lambda tid: h.trades.get(tid)

    def _get_statuses(sigs, **kw):
        return SimpleNamespace(value=[h.statuses.get(sigs[0])])

    def _get_transaction(sig, **kw):
        return SimpleNamespace(value=h.transactions.get(str(sig)))

    solana_mock = MagicMock()
    solana_mock.get_signature_statuses.side_effect = _get_statuses
    solana_mock.get_transaction.side_effect = _get_transaction

    h.db = MagicMock()
    h.app = Flask("test")
    h.logger = MagicMock()

    with patch.object(buy, "TradeHistory", history_model), \
         patch.object(buy, "Trade", trade_model), \
         patch.object(buy, "db", h.db), \
         patch.object(buy, "solana_client", solana_mock), \
         patch.object(buy, "Signature", SimpleNamespace(from_string=lambda s: s)), \
         patch.object(buy, "logger", h.logger), \
         patch.object(buy, "get_sol_usd_rate", return_value=200.0), \
         patch.object(buy, "SNIPER_BUY_RECONCILE_TIMEOUT_SEC", 90.0):
        yield h


# --- _actual_tokens_received -------------------------------------------------


def test_actual_tokens_received_computes_post_minus_pre():
    tx = _tx_data(
        post_balances=[_balance(MINT, OWNER, 1000.0)],
        pre_balances=[_balance(MINT, OWNER, 0.0)],
    )
    assert buy._actual_tokens_received(tx, MINT, OWNER) == pytest.approx(1000.0)


def test_actual_tokens_received_ignores_other_owners():
    other_owner = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    tx = _tx_data(
        post_balances=[_balance(MINT, other_owner, 5000.0),
                       _balance(MINT, OWNER, 1000.0)],
        pre_balances=[_balance(MINT, OWNER, 0.0)],
    )
    assert buy._actual_tokens_received(tx, MINT, OWNER) == pytest.approx(1000.0)


def test_actual_tokens_received_no_pre_balance_means_starting_from_zero():
    tx = _tx_data(post_balances=[_balance(MINT, OWNER, 500.0)], pre_balances=[])
    assert buy._actual_tokens_received(tx, MINT, OWNER) == pytest.approx(500.0)


def test_actual_tokens_received_missing_post_balance_returns_none():
    tx = _tx_data(post_balances=[], pre_balances=[])
    assert buy._actual_tokens_received(tx, MINT, OWNER) is None


def test_actual_tokens_received_malformed_tx_returns_none():
    assert buy._actual_tokens_received(SimpleNamespace(), MINT, OWNER) is None


# --- _apply_buy_price_reconciliation -----------------------------------------


def test_apply_reconciliation_corrects_price_and_quantity(harness):
    trade = _trade(amount=0.5, purchased_token_amount=900.0, initial_token_amount=900.0, tp_pct_sold=0.0)
    buy._apply_buy_price_reconciliation(trade, sol_spent=0.5, actual_tokens=1000.0)
    assert trade.initial_price == pytest.approx((0.5 * 200.0) / 1000.0)
    assert trade.purchased_token_amount == pytest.approx(1000.0)
    assert trade.initial_token_amount == pytest.approx(1000.0)


def test_apply_reconciliation_skipped_when_trade_already_executed(harness):
    trade = _trade(executed=True, initial_price=0.0, purchased_token_amount=900.0)
    buy._apply_buy_price_reconciliation(trade, sol_spent=0.5, actual_tokens=1000.0)
    assert trade.initial_price == 0.0  # untouched
    assert trade.purchased_token_amount == 900.0  # untouched


def test_apply_reconciliation_leaves_quantity_alone_if_partial_sell_already_happened(harness):
    """A partial sell already reduced purchased_token_amount based on the old
    estimate - overwriting it with the full actual amount would silently
    re-credit tokens that were already sold."""
    trade = _trade(purchased_token_amount=450.0, initial_token_amount=900.0, tp_pct_sold=50.0)
    buy._apply_buy_price_reconciliation(trade, sol_spent=0.5, actual_tokens=1000.0)
    assert trade.purchased_token_amount == 450.0  # untouched
    assert trade.initial_token_amount == 900.0  # untouched
    assert trade.initial_price == pytest.approx((0.5 * 200.0) / 1000.0)  # price still set


def test_apply_reconciliation_handles_sol_usd_lookup_failure(harness):
    with patch.object(buy, "get_sol_usd_rate", return_value=None):
        trade = _trade()
        buy._apply_buy_price_reconciliation(trade, sol_spent=0.5, actual_tokens=1000.0)
    assert trade.initial_price == 0.0  # left at placeholder, not crashed


def test_apply_reconciliation_uses_direct_pyth_rate_not_shyft(harness):
    """Regression guard for the actual requirement."""
    with patch.object(buy, "get_shyft_pricing_service") as shyft_svc:
        trade = _trade()
        buy._apply_buy_price_reconciliation(trade, sol_spent=0.5, actual_tokens=1000.0)
    shyft_svc.assert_not_called()


# --- reconcile_pending_buys end-to-end ---------------------------------------


def test_no_pending_buys_is_a_noop(harness):
    buy.reconcile_pending_buys(harness.app)
    harness.db.session.commit.assert_not_called()


def test_confirmed_buy_sets_price_and_marks_confirmed(harness):
    history = _history(tx_id="sig1", trade_id=5, amount=0.5)
    trade = _trade(id=5, amount=0.5, purchased_token_amount=900.0, initial_token_amount=900.0)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status()}
    harness.transactions = {"sig1": _tx_data(
        post_balances=[_balance(MINT, OWNER, 1000.0)], pre_balances=[],
    )}

    buy.reconcile_pending_buys(harness.app)

    assert history.status == "confirmed"
    assert trade.purchased_token_amount == pytest.approx(1000.0)
    assert trade.initial_price == pytest.approx((0.5 * 200.0) / 1000.0)
    assert history.execution_price == trade.initial_price


def test_failed_onchain_buy_leaves_placeholder_price_and_marks_failed(harness):
    history = _history(tx_id="sig1", trade_id=5)
    trade = _trade(id=5, initial_price=0.0)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status(err={"InstructionError": [0, "Custom"]})}

    buy.reconcile_pending_buys(harness.app)

    assert history.status == "failed"
    assert trade.initial_price == 0.0  # not touched - this job only fills in pricing


def test_unresolved_signature_within_timeout_stays_pending(harness):
    history = _history(tx_id="sig1", timestamp=datetime.utcnow())
    harness.history_rows = [history]
    harness.trades = {1: _trade()}
    harness.statuses = {}  # not seen yet

    buy.reconcile_pending_buys(harness.app)

    assert history.status == "sent"


def test_unresolved_signature_past_timeout_marked_failed_timeout(harness):
    history = _history(tx_id="sig1", trade_id=5, timestamp=datetime.utcnow() - timedelta(seconds=200))
    harness.history_rows = [history]
    harness.trades = {5: _trade(id=5)}
    harness.statuses = {}

    buy.reconcile_pending_buys(harness.app)

    assert history.status == "failed_timeout"


def test_missing_trade_row_still_resolves_history_status(harness):
    history = _history(tx_id="sig1", trade_id=999)
    harness.history_rows = [history]
    harness.trades = {}  # trade not found
    harness.statuses = {"sig1": _status()}

    buy.reconcile_pending_buys(harness.app)

    assert history.status == "confirmed"


def test_unparseable_transaction_within_timeout_retries_later(harness):
    """Regression: get_signature_statuses reporting confirmed can transiently
    race ahead of get_transaction actually returning complete balance data
    for that same signature. A miss here must not be treated as permanent -
    it must stay "sent" so a later cycle (once the data is actually
    available) can still succeed."""
    history = _history(tx_id="sig1", trade_id=5, timestamp=datetime.utcnow())
    trade = _trade(id=5, initial_price=0.0)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status()}
    harness.transactions = {"sig1": _tx_data(post_balances=[], pre_balances=[])}

    buy.reconcile_pending_buys(harness.app)

    assert history.status == "sent"  # retried later, not given up on yet
    assert trade.initial_price == 0.0  # untouched - placeholder kept


def test_unparseable_transaction_past_timeout_gives_up(harness):
    """Once the bounded timeout elapses, stop retrying for good."""
    history = _history(tx_id="sig1", trade_id=5, timestamp=datetime.utcnow() - timedelta(seconds=200))
    trade = _trade(id=5, initial_price=0.0)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status()}
    harness.transactions = {"sig1": _tx_data(post_balances=[], pre_balances=[])}

    buy.reconcile_pending_buys(harness.app)

    assert history.status == "confirmed"
    assert trade.initial_price == 0.0  # untouched - placeholder kept


def test_unparseable_then_parseable_on_a_later_cycle_succeeds(harness):
    """End-to-end of the actual bug found in production: first cycle can't
    parse the balance delta yet (transient), second cycle (data now
    available) correctly computes the price."""
    history = _history(tx_id="sig1", trade_id=5, timestamp=datetime.utcnow())
    trade = _trade(id=5, amount=0.5, initial_price=0.0, purchased_token_amount=900.0)
    harness.history_rows = [history]
    harness.trades = {5: trade}
    harness.statuses = {"sig1": _status()}
    harness.transactions = {"sig1": _tx_data(post_balances=[], pre_balances=[])}  # not available yet

    buy.reconcile_pending_buys(harness.app)
    assert history.status == "sent"
    assert trade.initial_price == 0.0

    # Data becomes available on the next cycle.
    harness.transactions = {"sig1": _tx_data(post_balances=[_balance(MINT, OWNER, 1000.0)], pre_balances=[])}
    buy.reconcile_pending_buys(harness.app)

    assert history.status == "confirmed"
    assert trade.initial_price == pytest.approx((0.5 * 200.0) / 1000.0)
    assert trade.purchased_token_amount == pytest.approx(1000.0)


def test_status_lookup_failure_leaves_row_pending_for_next_run(harness):
    history = _history(tx_id="sig1")
    harness.history_rows = [history]
    harness.trades = {1: _trade()}

    with patch.object(buy.solana_client, "get_signature_statuses", side_effect=RuntimeError("RPC down")):
        buy.reconcile_pending_buys(harness.app)

    assert history.status == "sent"


def test_multiple_pending_buys_each_resolved_independently(harness):
    confirmed = _history(id=1, tx_id="sigA", trade_id=1, amount=0.5)
    failed = _history(id=2, tx_id="sigB", trade_id=2, amount=0.3)
    harness.history_rows = [confirmed, failed]
    harness.trades = {
        1: _trade(id=1, amount=0.5, purchased_token_amount=900.0),
        2: _trade(id=2, amount=0.3, initial_price=0.0),
    }
    harness.statuses = {
        "sigA": _status(),
        "sigB": _status(err="any"),
    }
    harness.transactions = {"sigA": _tx_data(post_balances=[_balance(MINT, OWNER, 1000.0)], pre_balances=[])}

    buy.reconcile_pending_buys(harness.app)

    assert confirmed.status == "confirmed"
    assert harness.trades[1].purchased_token_amount == pytest.approx(1000.0)
    assert failed.status == "failed"
    assert harness.trades[2].initial_price == 0.0


def test_signature_status_lookup_uses_search_transaction_history(harness):
    """Regression guard: without search_transaction_history=True, the RPC
    node's short-lived recent-status cache can evict an older signature and
    return None even for a transaction that actually finalized, eventually
    mislabeling a successful buy as failed_timeout."""
    history = _history(tx_id="sig1", trade_id=1)
    harness.history_rows = [history]
    harness.trades = {1: _trade()}
    harness.statuses = {"sig1": _status()}

    with patch.object(buy.solana_client, "get_signature_statuses", wraps=lambda sigs, **kw: SimpleNamespace(
        value=[harness.statuses.get(sigs[0])]
    )) as get_statuses:
        buy.reconcile_pending_buys(harness.app)

    assert get_statuses.call_args.kwargs.get("search_transaction_history") is True


def test_only_buy_trade_type_rows_are_considered(harness):
    """filter_by(trade_type="BUY") must exclude sell rows sharing the same
    status="sent" value - the mock harness's filter already encodes this,
    this test just makes sure a non-BUY row genuinely gets skipped."""
    sell_row = _history(trade_type="SELL", tx_id="sigSell")
    harness.history_rows = [sell_row]
    harness.statuses = {"sigSell": _status()}

    buy.reconcile_pending_buys(harness.app)

    assert sell_row.status == "sent"  # never touched - not a BUY row
