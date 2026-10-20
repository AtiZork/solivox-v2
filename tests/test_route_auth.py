"""Security regression tests: every trade/wallet/config-mutating route must
require a JWT and must only act on the caller's own rows.

These were added as part of the auth-hardening pass - before it, most of the
routes tested here (/trade/<id>/, /buy_token/<id>/, /sell_token/<id>/,
/api/update_auto_sell/<id>, /token_stats/<id>, /trade_history/<id>,
/api/token/prices, /start_all, /stop_all) had no @jwt_required() at all and
no ownership check, so any caller (or any logged-in user, for the ownership
gap) could act on another user's trade/wallet.

Builds its own minimal Flask app registering only the blueprints under test,
against an in-memory SQLite DB, so it does not need Postgres or the full
main.py import chain (which pulls in packages - bip_utils/ed25519-blake2b -
that fail to build in this environment; see PR notes).
"""
import sys
from unittest.mock import patch

import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from models import db, User, Trade


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["JWT_SECRET_KEY"] = "test-secret-key-at-least-32-bytes-long"
    db.init_app(app)
    JWTManager(app)

    import trade
    import menual_sell_token
    import token_price
    import trade_history
    import autosnipe
    import monitor
    import charts
    import user_register
    import autosnipe_buy_new_token
    import live_pricing
    import trades_api
    import dashboard_api

    app.register_blueprint(trade.solana_bp)
    app.register_blueprint(menual_sell_token.manual_sell_trade_bp)
    app.register_blueprint(token_price.token_price_bp)
    app.register_blueprint(trade_history.trade_history_bp)
    app.register_blueprint(autosnipe.autosnipe_bp)
    # Registered for test_every_non_public_route_requires_auth below (action-
    # plan 1.7) - not needed by any test above this point. wallet_managment's
    # wallet_bp is the one blueprint main.py registers that's left out: it
    # imports bip_utils, which doesn't build in this environment (see the
    # module docstring) - so it has no coverage from the enumeration test.
    app.register_blueprint(monitor.monitor_bp)
    app.register_blueprint(charts.charts_bp)
    app.register_blueprint(user_register.user_register_bp)
    app.register_blueprint(autosnipe_buy_new_token.autosnipe_trade_bp)
    app.register_blueprint(live_pricing.live_pricing_bp)
    # Action-plan 3.1/3.2 - new routes, same auth-enumeration coverage.
    app.register_blueprint(trades_api.trades_api_bp)
    app.register_blueprint(dashboard_api.dashboard_api_bp)

    with app.app_context():
        db.create_all()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(app, username):
    with app.app_context():
        user = User(username=username, email=f"{username}@example.com")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        return user.id


def _make_trade(app, user_id, **overrides):
    with app.app_context():
        fields = dict(
            to_pubkey="OwnerPubkey111111111111111111111111111111",
            user_id=user_id,
            amount=1.0,
            initial_price=1.0,
            token_address="TokenMint1111111111111111111111111111111",
            token_name="Test Token",
            purchased_token_amount=100.0,
            long_sell_slippage=1000,
            long_sell_gas_fee=0.01,
            default_gas_fee=0.01,
            long_slippage=1000,
        )
        fields.update(overrides)
        trade = Trade(**fields)
        db.session.add(trade)
        db.session.commit()
        return trade.id


def _token(app, user_id):
    with app.app_context():
        return create_access_token(identity=str(user_id))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# PUT /trade/<id>/  (trade.py:update_trade)
# ---------------------------------------------------------------------------

def test_update_trade_requires_auth(client, app):
    trade_id = _make_trade(app, _make_user(app, "alice"))
    resp = client.put(f"/trade/{trade_id}/", json={"buy_slippage": 5})
    assert resp.status_code == 401


def test_update_trade_rejects_other_users_trade(client, app):
    owner_id = _make_user(app, "alice")
    other_id = _make_user(app, "bob")
    trade_id = _make_trade(app, owner_id)
    resp = client.put(
        f"/trade/{trade_id}/",
        json={"buy_slippage": 5},
        headers=_auth(_token(app, other_id)),
    )
    assert resp.status_code == 404


def test_update_trade_allows_owner(client, app):
    owner_id = _make_user(app, "alice")
    trade_id = _make_trade(app, owner_id)
    resp = client.put(
        f"/trade/{trade_id}/",
        json={"buy_slippage": 5},
        headers=_auth(_token(app, owner_id)),
    )
    assert resp.status_code == 200
    with app.app_context():
        assert Trade.query.get(trade_id).long_slippage == 5


# ---------------------------------------------------------------------------
# GET /token_stats/<id>  (trade.py:token_stats)
# ---------------------------------------------------------------------------

def test_token_stats_requires_auth(client, app):
    trade_id = _make_trade(app, _make_user(app, "alice"))
    assert client.get(f"/token_stats/{trade_id}").status_code == 401


def test_token_stats_rejects_other_users_trade(client, app):
    owner_id = _make_user(app, "alice")
    other_id = _make_user(app, "bob")
    trade_id = _make_trade(app, owner_id)
    resp = client.get(f"/token_stats/{trade_id}", headers=_auth(_token(app, other_id)))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /buy_token/<id>/  (trade.py:buy_token)
# ---------------------------------------------------------------------------

def test_buy_token_requires_auth(client, app):
    trade_id = _make_trade(app, _make_user(app, "alice"))
    assert client.put(f"/buy_token/{trade_id}/", json={"amount": 0.1}).status_code == 401


def test_buy_token_rejects_other_users_trade(client, app):
    owner_id = _make_user(app, "alice")
    other_id = _make_user(app, "bob")
    trade_id = _make_trade(app, owner_id)
    resp = client.put(
        f"/buy_token/{trade_id}/",
        json={"amount": 0.1},
        headers=_auth(_token(app, other_id)),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/update_auto_sell/<id>  (trade.py:update_auto_sell)
# ---------------------------------------------------------------------------

def test_update_auto_sell_requires_auth(client, app):
    trade_id = _make_trade(app, _make_user(app, "alice"))
    resp = client.post(f"/api/update_auto_sell/{trade_id}", json={"auto_sell": True})
    assert resp.status_code == 401


def test_update_auto_sell_rejects_other_users_trade(client, app):
    owner_id = _make_user(app, "alice")
    other_id = _make_user(app, "bob")
    trade_id = _make_trade(app, owner_id)
    resp = client.post(
        f"/api/update_auto_sell/{trade_id}",
        json={"auto_sell": True},
        headers=_auth(_token(app, other_id)),
    )
    assert resp.status_code == 404


def test_update_auto_sell_allows_owner(client, app):
    owner_id = _make_user(app, "alice")
    trade_id = _make_trade(app, owner_id)
    resp = client.post(
        f"/api/update_auto_sell/{trade_id}",
        json={"auto_sell": True},
        headers=_auth(_token(app, owner_id)),
    )
    assert resp.status_code == 200
    with app.app_context():
        assert Trade.query.get(trade_id).auto_sell is True


# ---------------------------------------------------------------------------
# PUT /sell_token/<id>/  (menual_sell_token.py:sell_token)
# ---------------------------------------------------------------------------

def test_sell_token_requires_auth(client, app):
    trade_id = _make_trade(app, _make_user(app, "alice"))
    resp = client.put(f"/sell_token/{trade_id}/", json={"sell_percent": 10})
    assert resp.status_code == 401


def test_sell_token_rejects_other_users_trade(client, app):
    owner_id = _make_user(app, "alice")
    other_id = _make_user(app, "bob")
    trade_id = _make_trade(app, owner_id)
    resp = client.put(
        f"/sell_token/{trade_id}/",
        json={"sell_percent": 10},
        headers=_auth(_token(app, other_id)),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /trade_history/<id>  (trade_history.py)
# ---------------------------------------------------------------------------

def test_trade_history_requires_auth(client, app):
    trade_id = _make_trade(app, _make_user(app, "alice"))
    assert client.get(f"/trade_history/{trade_id}").status_code == 401


def test_trade_history_rejects_other_users_trade(client, app):
    owner_id = _make_user(app, "alice")
    other_id = _make_user(app, "bob")
    trade_id = _make_trade(app, owner_id)
    resp = client.get(f"/trade_history/{trade_id}", headers=_auth(_token(app, other_id)))
    assert resp.status_code == 404


def test_trade_history_allows_owner(client, app):
    owner_id = _make_user(app, "alice")
    trade_id = _make_trade(app, owner_id)
    resp = client.get(f"/trade_history/{trade_id}", headers=_auth(_token(app, owner_id)))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/token/prices  (token_price.py) - previously returned every
# user's trades with no auth at all.
# ---------------------------------------------------------------------------

def test_token_prices_requires_auth(client, app):
    assert client.get("/api/token/prices").status_code == 401


def test_token_prices_scoped_to_caller(client, app):
    owner_id = _make_user(app, "alice")
    other_id = _make_user(app, "bob")
    _make_trade(app, owner_id, token_address="AliceToken1111111111111111111111111111111")
    _make_trade(app, other_id, token_address="BobToken11111111111111111111111111111111")

    resp = client.get("/api/token/prices", headers=_auth(_token(app, owner_id)))
    assert resp.status_code == 200
    addresses = [row["token_address"] for row in resp.get_json()]
    assert addresses == ["AliceToken1111111111111111111111111111111"]


# ---------------------------------------------------------------------------
# /start_all, /stop_all (autosnipe.py) - retired: previously unauthenticated
# and started a buy bot that could act on an arbitrary user's wallet/config.
# ---------------------------------------------------------------------------

def test_start_all_is_retired(client):
    resp = client.post("/start_all")
    assert resp.status_code == 410


def test_stop_all_is_retired(client):
    resp = client.post("/stop_all")
    assert resp.status_code == 410


# ---------------------------------------------------------------------------
# POST /trade (trade.py:buy_token_trade) - to_pubkey was trusted from the
# request body with no ownership filter, so a logged-in user could make the
# bot buy through any wallet in the DB, not just their own.
# ---------------------------------------------------------------------------

def test_buy_now_rejects_wallet_owned_by_another_user(client, app):
    from models import Wallet

    owner_id = _make_user(app, "alice")
    attacker_id = _make_user(app, "mallory")
    with app.app_context():
        wallet = Wallet(
            public_key="VictimWallet111111111111111111111111111111",
            private_key_path="C:/does/not/matter.key",
            title="alice-wallet",
            user_id=owner_id,
        )
        db.session.add(wallet)
        db.session.commit()

    with patch("trade.get_token_price", return_value={"usdPrice": 1.0, "name": "T", "symbol": "T"}):
        resp = client.post(
            "/trade",
            json={
                "to_pubkey": "VictimWallet111111111111111111111111111111",
                "token_address": "TokenMint1111111111111111111111111111111",
                "amount": 0.1,
                "buy_now": True,
            },
            headers=_auth(_token(app, attacker_id)),
        )
    assert resp.status_code == 400
    assert "wallet" in resp.get_json().get("message", "").lower()


# ---------------------------------------------------------------------------
# Action-plan 1.7: route-auth enumeration.
#
# Every test above only covers the specific routes it names - nothing fails
# if a *new* route is added later without @jwt_required(). This walks every
# rule Flask actually knows about and asserts each one 401s with no token,
# except an explicit allowlist of routes that have no per-user ownership
# concept at all (public market-data proxies, health checks, the auth
# bootstrap endpoints themselves, and endpoints already retired to a fixed
# non-200 status regardless of auth).
# ---------------------------------------------------------------------------

# (endpoint, method) pairs that legitimately don't require a JWT. Keep each
# entry's reason next to it - this allowlist is the thing a reviewer actually
# needs to re-check by hand, since nothing else on this test enforces that a
# new entry here is still a good idea.
PUBLIC_ROUTES = {
    # Auth bootstrap - must be callable with no token by definition.
    ("user_register_bp.register", "POST"),
    ("user_register_bp.login", "POST"),
    # Retired, fixed non-200 response regardless of auth - already locked
    # down by test_start_all_is_retired/test_stop_all_is_retired above.
    ("autosnipe_bp.start_all_bots", "POST"),
    ("autosnipe_bp.stop_all_bots", "POST"),
    # Retired, unconditional 404 - see monitor.py's own docstring.
    ("monitor.monitor", "GET"),
    # Global market-data proxies (DexScreener/CoinGecko by mint address) -
    # no user_id/trade_id involved, nothing here is per-user data.
    ("charts_bp.index", "GET"),
    ("charts_bp.chart", "GET"),
    ("charts_bp.token_prices", "GET"),
    # Global health checks for the sniper/price WebSocket listeners - process-
    # wide status (booleans, counters), not scoped to any user.
    ("autosnipe_trade_bp.sniper_stream_status", "GET"),
    ("autosnipe_trade_bp.price_stream_status_api", "GET"),
    # Global on-chain DEX pool price lookups - same "no per-user data" class
    # as charts_bp above. NOTE: /refresh_pools and /register_pool go further
    # than a read-only lookup (they trigger a background refresh and write to
    # the shared on-disk pool registry, respectively) with no auth at all -
    # flagged to the user as a real finding from writing this test, not
    # something this test is fixing. Allowlisted here because that's what
    # they actually do today; tightening them is a separate decision.
    ("live_pricing_bp.live_token_price", "GET"),
    ("live_pricing_bp.manual_refresh_pools", "POST"),
    ("live_pricing_bp.pump_price", "POST"),
    ("live_pricing_bp.price_from_vaults", "GET"),
    ("live_pricing_bp.register_pool", "POST"),
    ("live_pricing_bp.raydium_price", "GET"),
}


def test_every_non_public_route_requires_auth(app, client):
    """
    For every route Flask knows about except PUBLIC_ROUTES above, calling it
    with no Authorization header must 401 - whatever the route otherwise
    does, that check must happen before anything else.

    wallet_managment.py's routes are NOT covered: that module imports
    bip_utils, which fails to build in this test environment (see the module
    docstring at the top of this file) - its auth is untested here. Routes
    main.py defines directly (page renders like "/", "/dashboard") are also
    out of scope: they're not on any blueprint, so the minimal app this file
    builds (for the same bip_utils reason) never sees them.
    """
    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = rule.methods - {"HEAD", "OPTIONS"}
        for method in methods:
            if (rule.endpoint, method) in PUBLIC_ROUTES:
                continue
            path = rule.build({arg: "1" for arg in rule.arguments}, append_unknown=False)[1]
            resp = client.open(path, method=method, json={})
            assert resp.status_code == 401, (
                f"{method} {path} ({rule.endpoint}) returned {resp.status_code} with no "
                f"Authorization header - expected 401, or an explicit PUBLIC_ROUTES entry "
                f"explaining why not."
            )
            checked += 1
    assert checked > 15  # sanity: fails loud if the blueprint list above ever shrinks silently
