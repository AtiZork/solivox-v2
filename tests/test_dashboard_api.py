"""
Action-plan 3.1/3.2/3.3: GET /api/trades, GET /api/dashboard/financial,
GET /api/dashboard/latency.

Covers the two things the plan's own "DONE WHEN" criteria care about most:
ownership (another user's trades must never leak - the exact bug class
Phase 1 fixed everywhere else) and honesty (sol_received/fees_paid_sol are
never a plausible-but-wrong number - see dashboard_api.py's module
docstring for why those aren't tracked).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from models import db, User, Trade, TradeHistory, Wallet, TokenPrice
import trades_api
import dashboard_api
import dashboard_models
from dashboard_models import LatencySnapshot
from latency_log import buy_stats, sell_stats

FAKE_SOL_USD_RATE = 200.0


@pytest.fixture(autouse=True)
def _no_real_sol_usd_rate_calls():
    """financial_summary() fetches a current SOL/USD rate for the
    approximate SOL-equivalent fields - real shyft_pricing.get_sol_usd()
    makes a live Solana RPC call, which would make every test here slow and
    network-dependent. Fixed, deterministic rate instead."""
    fake_service = MagicMock()
    fake_service.get_sol_usd.return_value = FAKE_SOL_USD_RATE
    with patch.object(dashboard_api, "get_shyft_pricing_service", return_value=fake_service):
        yield


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["JWT_SECRET_KEY"] = "test-secret-key-at-least-32-bytes-long"
    db.init_app(app)
    JWTManager(app)
    app.register_blueprint(trades_api.trades_api_bp)
    app.register_blueprint(dashboard_api.dashboard_api_bp)
    with app.app_context():
        db.create_all()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _user(app, username):
    with app.app_context():
        u = User(username=username, email=f"{username}@example.com")
        u.set_password("password123")
        db.session.add(u)
        db.session.commit()
        return u.id


def _token(app, user_id):
    with app.app_context():
        return create_access_token(identity=str(user_id))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _trade(app, user_id, **overrides):
    with app.app_context():
        fields = dict(
            to_pubkey="Wallet111111111111111111111111111111111111",
            user_id=user_id,
            amount=1.0,
            initial_price=0.01,
            token_address="Mint1111111111111111111111111111111111111",
            purchased_token_amount=0.0,
            executed=True,
            trade_kind="AUTOSNIPE",
            created_at=datetime.utcnow(),
        )
        fields.update(overrides)
        trade = Trade(**fields)
        db.session.add(trade)
        db.session.commit()
        return trade.id


def _sell_history(app, trade_id, **overrides):
    with app.app_context():
        fields = dict(
            trade_id=trade_id,
            token_address="Mint1111111111111111111111111111111111111",
            trade_type="SELL",
            trade_kind="AutoSnipe",
            amount=100.0,
            execution_price=0.02,
            tx_id="sig1",
            status="confirmed",
            timestamp=datetime.utcnow(),
        )
        fields.update(overrides)
        db.session.add(TradeHistory(**fields))
        db.session.commit()


# --- /api/trades: ownership and pagination ---------------------------------


def test_trades_list_never_includes_another_users_trade(client, app):
    owner_id = _user(app, "alice")
    other_id = _user(app, "mallory")
    _trade(app, owner_id)
    _trade(app, other_id, token_address="Mint2222222222222222222222222222222222222")

    resp = client.get("/api/trades", headers=_auth(_token(app, owner_id)))

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1
    assert len(data["trades"]) == 1


def test_trades_page_size_is_capped(client, app):
    owner_id = _user(app, "alice")
    for i in range(5):
        _trade(app, owner_id, token_address=f"Mint{i}11111111111111111111111111111111111")

    resp = client.get(f"/api/trades?page_size={trades_api.MAX_PAGE_SIZE + 500}", headers=_auth(_token(app, owner_id)))

    assert resp.status_code == 200
    assert resp.get_json()["page_size"] == trades_api.MAX_PAGE_SIZE


def test_trades_wallet_filter_rejects_another_users_wallet(client, app):
    owner_id = _user(app, "alice")
    other_id = _user(app, "mallory")
    with app.app_context():
        other_wallet = Wallet(public_key="Other1111111111111111111111111111111111111",
                               private_key_path="k", title="t", user_id=other_id)
        db.session.add(other_wallet)
        db.session.commit()
        other_wallet_id = other_wallet.id
    _trade(app, owner_id)

    resp = client.get(f"/api/trades?wallet_id={other_wallet_id}", headers=_auth(_token(app, owner_id)))

    assert resp.status_code == 404


def test_realized_pnl_prices_each_partial_sell_correctly(client, app):
    """Two tiered take-profit sells at different prices - cost basis must be
    charged against the tokens each sell actually sold, not the whole
    position at one price."""
    owner_id = _user(app, "alice")
    trade_id = _trade(app, owner_id, initial_price=0.01, executed=True)
    _sell_history(app, trade_id, amount=50.0, execution_price=0.02, tx_id="sig1")  # +0.5
    _sell_history(app, trade_id, amount=50.0, execution_price=0.04, tx_id="sig2")  # +1.5

    resp = client.get("/api/trades", headers=_auth(_token(app, owner_id)))

    row = resp.get_json()["trades"][0]
    assert row["realized_pnl_usd"] == pytest.approx(2.0)
    assert row["outcome"] == "win"


def test_open_trades_only_appear_under_status_open(client, app):
    """Regression guard for the bug caught before shipping: filtering status
    in Python after offset/limit would under-report `total` and could drop
    rows from a page. `total` and the returned row count must agree."""
    owner_id = _user(app, "alice")
    _trade(app, owner_id, executed=True)
    _trade(app, owner_id, executed=False, token_address="Mint2222222222222222222222222222222222222")

    resp = client.get("/api/trades?status=open", headers=_auth(_token(app, owner_id)))

    data = resp.get_json()
    assert data["total"] == 1
    assert len(data["trades"]) == 1
    assert data["trades"][0]["status"] == "open"


# --- /api/trades/log: client-requested raw transaction log -----------------


def test_transaction_log_never_includes_another_users_event(client, app):
    owner_id = _user(app, "alice")
    other_id = _user(app, "mallory")
    owner_trade = _trade(app, owner_id)
    other_trade = _trade(app, other_id, token_address="Mint2222222222222222222222222222222222222")
    _sell_history(app, owner_trade, tx_id="sigOwner")
    _sell_history(app, other_trade, tx_id="sigOther")

    resp = client.get("/api/trades/log", headers=_auth(_token(app, owner_id)))

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1
    assert data["events"][0]["tx_id"] == "sigOwner"


def test_transaction_log_includes_both_buy_and_sell_events(client, app):
    owner_id = _user(app, "alice")
    trade_id = _trade(app, owner_id)
    with app.app_context():
        db.session.add(TradeHistory(
            trade_id=trade_id, token_address="Mint1111111111111111111111111111111111111",
            trade_type="BUY", trade_kind="AutoSnipe", amount=1.0, execution_price=0.01,
            tx_id="sigBuy", status="confirmed", timestamp=datetime.utcnow(),
        ))
        db.session.commit()
    _sell_history(app, trade_id, tx_id="sigSell")

    resp = client.get("/api/trades/log", headers=_auth(_token(app, owner_id)))

    types = {e["trade_type"] for e in resp.get_json()["events"]}
    assert types == {"BUY", "SELL"}


def test_transaction_log_filters_by_trade_type(client, app):
    owner_id = _user(app, "alice")
    trade_id = _trade(app, owner_id)
    with app.app_context():
        db.session.add(TradeHistory(
            trade_id=trade_id, token_address="Mint1111111111111111111111111111111111111",
            trade_type="BUY", trade_kind="AutoSnipe", amount=1.0, execution_price=0.01,
            tx_id="sigBuy", status="confirmed", timestamp=datetime.utcnow(),
        ))
        db.session.commit()
    _sell_history(app, trade_id, tx_id="sigSell")

    resp = client.get("/api/trades/log?trade_type=SELL", headers=_auth(_token(app, owner_id)))

    data = resp.get_json()
    assert data["total"] == 1
    assert data["events"][0]["trade_type"] == "SELL"


def test_transaction_log_page_size_is_capped(client, app):
    owner_id = _user(app, "alice")
    trade_id = _trade(app, owner_id)
    for i in range(3):
        _sell_history(app, trade_id, tx_id=f"sig{i}")

    resp = client.get(
        f"/api/trades/log?page_size={trades_api.MAX_PAGE_SIZE + 500}", headers=_auth(_token(app, owner_id))
    )

    assert resp.get_json()["page_size"] == trades_api.MAX_PAGE_SIZE


def test_transaction_log_null_trade_id_row_is_never_returned_to_anyone(client, app):
    """TradeHistory.trade_id is nullable - a row with no trade can't be
    attributed to any user, so it must never appear, not even to the trade
    owner (there is no owner)."""
    owner_id = _user(app, "alice")
    _trade(app, owner_id)
    with app.app_context():
        db.session.add(TradeHistory(
            trade_id=None, token_address="Orphan11111111111111111111111111111111111",
            trade_type="SELL", trade_kind="AutoSnipe", amount=1.0, execution_price=0.01,
            tx_id="sigOrphan", status="confirmed", timestamp=datetime.utcnow(),
        ))
        db.session.commit()

    resp = client.get("/api/trades/log", headers=_auth(_token(app, owner_id)))

    tx_ids = {e["tx_id"] for e in resp.get_json()["events"]}
    assert "sigOrphan" not in tx_ids


# --- /api/dashboard/financial: ownership and honesty ------------------------


def test_financial_summary_scoped_to_caller(client, app):
    owner_id = _user(app, "alice")
    other_id = _user(app, "mallory")
    _trade(app, owner_id, amount=1.0)
    _trade(app, other_id, amount=99.0, token_address="Mint2222222222222222222222222222222222222")

    resp = client.get("/api/dashboard/financial?status=all", headers=_auth(_token(app, owner_id)))

    assert resp.get_json()["totals"]["sol_spent"] == pytest.approx(1.0)


def test_sol_received_and_fees_are_explicitly_not_tracked(client, app):
    owner_id = _user(app, "alice")
    trade_id = _trade(app, owner_id)
    _sell_history(app, trade_id)

    resp = client.get("/api/dashboard/financial", headers=_auth(_token(app, owner_id)))

    totals = resp.get_json()["totals"]
    assert totals["sol_received"] is None
    assert totals["fees_paid_sol"] is None
    assert totals["not_tracked"] is True
    # What IS tracked must still be real numbers, not also nulled out.
    assert totals["sol_spent"] == pytest.approx(1.0)
    assert totals["realized_pnl_usd"] == pytest.approx(1.0)


def test_earned_and_pnl_sol_approx_use_the_current_sol_usd_rate(client, app):
    """Client feedback: show Earned (and P&L) also as an approximate SOL
    figure, clearly distinct from the exact `sol_received`/`realized_pnl_sol`
    (still null/not_tracked - this is a current-rate conversion of the
    exact USD figures, not a claim of exact SOL tracking)."""
    owner_id = _user(app, "alice")
    trade_id = _trade(app, owner_id, initial_price=0.01)
    _sell_history(app, trade_id, amount=100.0, execution_price=0.02)  # earned_usd=2.0, pnl_usd=1.0

    resp = client.get("/api/dashboard/financial", headers=_auth(_token(app, owner_id)))

    totals = resp.get_json()["totals"]
    assert totals["sol_usd_rate_used"] == pytest.approx(FAKE_SOL_USD_RATE)
    assert totals["earned_sol_approx"] == pytest.approx(2.0 / FAKE_SOL_USD_RATE)
    assert totals["realized_pnl_sol_approx"] == pytest.approx(1.0 / FAKE_SOL_USD_RATE)
    # The exact SOL fields must stay null regardless - the approx fields are
    # additive, not a replacement for the still-missing exact tracking.
    assert totals["sol_received"] is None
    assert totals["realized_pnl_sol"] is None


def test_sol_spent_also_shown_as_approximate_usd(client, app):
    """Follow-up client feedback: Spent was SOL-only - also show its USD
    equivalent (same current-rate-conversion approach as earned_sol_approx,
    just the other direction)."""
    owner_id = _user(app, "alice")
    _trade(app, owner_id, amount=0.9)

    resp = client.get("/api/dashboard/financial?status=all", headers=_auth(_token(app, owner_id)))

    totals = resp.get_json()["totals"]
    assert totals["sol_spent"] == pytest.approx(0.9)
    assert totals["sol_spent_usd_approx"] == pytest.approx(0.9 * FAKE_SOL_USD_RATE)


def test_sol_approx_fields_are_null_when_the_rate_fetch_fails(client, app):
    """A down/unreachable Pyth feed must degrade to null approx fields, not
    break the rest of the (exact, more important) summary."""
    owner_id = _user(app, "alice")
    _trade(app, owner_id, amount=1.0)
    broken_service = MagicMock()
    broken_service.get_sol_usd.return_value = None

    with patch.object(dashboard_api, "get_shyft_pricing_service", return_value=broken_service):
        resp = client.get("/api/dashboard/financial?status=all", headers=_auth(_token(app, owner_id)))

    assert resp.status_code == 200
    totals = resp.get_json()["totals"]
    assert totals["sol_usd_rate_used"] is None
    assert totals["earned_sol_approx"] is None
    assert totals["sol_spent_usd_approx"] is None
    assert totals["sol_spent"] == pytest.approx(1.0)  # exact figures unaffected


def test_unrealized_pnl_uses_latest_known_token_price(client, app):
    owner_id = _user(app, "alice")
    trade_id = _trade(app, owner_id, executed=False, initial_price=0.01, purchased_token_amount=100.0)
    with app.app_context():
        db.session.add(TokenPrice(trade_id=trade_id, token_address="x", price=0.015))
        db.session.commit()

    resp = client.get("/api/dashboard/financial?status=open", headers=_auth(_token(app, owner_id)))

    assert resp.get_json()["totals"]["unrealized_pnl_usd"] == pytest.approx(0.5)


def test_by_day_and_totals_agree_under_the_same_status_filter(client, app):
    """Regression guard: by_day/by_wallet must respect the same status
    filter as totals - they were computed over ALL trades regardless of
    `status` in an earlier draft, which made a "confirmed only" summary's
    breakdown silently include open positions."""
    owner_id = _user(app, "alice")
    _trade(app, owner_id, executed=True, amount=1.0)
    _trade(app, owner_id, executed=False, amount=5.0, token_address="Mint2222222222222222222222222222222222222")

    resp = client.get("/api/dashboard/financial?status=confirmed", headers=_auth(_token(app, owner_id)))

    data = resp.get_json()
    assert data["totals"]["sol_spent"] == pytest.approx(1.0)
    assert sum(d["sol_spent"] for d in data["by_day"]) == pytest.approx(1.0)
    assert sum(d["sol_spent"] for d in data["by_wallet"]) == pytest.approx(1.0)


# --- /api/dashboard/latency --------------------------------------------------


def test_latency_endpoint_requires_no_ownership_filter_but_still_auth(client, app):
    owner_id = _user(app, "alice")
    resp = client.get("/api/dashboard/latency", headers=_auth(_token(app, owner_id)))
    assert resp.status_code == 200
    assert resp.get_json()["series"] == []


def test_latency_snapshot_written_on_forced_flush(app):
    """Action-plan 3.3: a buy/sell StageTimer completion feeds buy_stats/
    sell_stats (latency_log.py), and flush_latency_snapshots persists
    whatever's accumulated into LatencySnapshot.

    buy_stats/sell_stats are process-wide singletons shared with every other
    test that exercises a real buy/sell StageTimer (e.g.
    test_buy_detection_aggregator_routing.py) - drained first so this test's
    count/avg/max assertions aren't contaminated by whatever ran earlier in
    the same pytest session.
    """
    buy_stats.flush_and_return(force=True)
    buy_stats.record("quote", 42.0)
    buy_stats.record("quote", 58.0)

    with app.app_context():
        dashboard_models.flush_latency_snapshots(app)
        rows = LatencySnapshot.query.filter_by(path="buy", stage="quote").all()

    assert len(rows) == 1
    assert rows[0].count == 2
    assert rows[0].avg_ms == pytest.approx(50.0)
    assert rows[0].max_ms == pytest.approx(58.0)


def test_stage_timer_finish_feeds_the_path_aggregator():
    """The actual wiring in StageTimer.finish() - not just that StageStats
    works in isolation."""
    from latency_log import StageTimer

    buy_stats.flush_and_return(force=True)  # drain whatever earlier tests left
    timer = StageTimer("buy", mint="X")
    with timer.stage("quote"):
        pass
    timer.finish("sent")

    snapshot = buy_stats.flush_and_return(force=True)
    assert snapshot is not None
    assert "quote" in snapshot["durations"]
    assert "total" in snapshot["durations"]
