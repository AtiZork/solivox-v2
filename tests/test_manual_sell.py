"""Regression tests for manual sell safety (C11).

Before this, PUT /sell_token/<id>/ had no per-trade lock (could race the
auto-sell bot into a double sell), trusted the DB's purchased_token_amount
instead of the wallet's real on-chain balance, crashed on 64-byte private
keys (from_seed() only accepts 32), and wrote no "sent" status row, so a
failed/dropped manual sell permanently reduced the position with nothing to
ever recover it.
"""
from __future__ import annotations

import base64
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from solders.keypair import Keypair

from models import db, User, Trade, Wallet, TradeHistory
import menual_sell_token as manual_sell


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
def app(tmp_path):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["JWT_SECRET_KEY"] = "test-secret-key-at-least-32-bytes-long"
    db.init_app(app)
    JWTManager(app)
    app.register_blueprint(manual_sell.manual_sell_trade_bp)

    with app.app_context():
        db.create_all()
        user = User(username="manual_sell_tester", email="m@test.local")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        app.test_user_id = user.id
        app.test_token = create_access_token(identity=str(user.id))

        seed = os.urandom(32)
        key_file = tmp_path / "wallet.key"
        key_file.write_bytes(seed)
        keypair = Keypair.from_seed(seed)
        _FakeVersionedTx.wallet_pubkey = keypair.pubkey()
        app.test_wallet_pubkey = str(keypair.pubkey())

        wallet = Wallet(
            public_key=app.test_wallet_pubkey, private_key_path=str(key_file),
            title="test", user_id=user.id,
        )
        db.session.add(wallet)
        db.session.commit()

        trade = Trade(
            to_pubkey=app.test_wallet_pubkey,
            user_id=user.id,
            amount=1.0,
            initial_price=1.0,
            # Must be a valid base58 pubkey - Pubkey.from_string(token_address)
            # is called on the real (unmocked) solders code before the token
            # supply lookup. Reuses the native SOL mint as a stand-in.
            token_address="So11111111111111111111111111111111111111112",
            token_name="Test Token",
            purchased_token_amount=1000.0,
            long_sell_slippage=20,
            long_sell_gas_fee=0.01,
            executed=False,
        )
        db.session.add(trade)
        db.session.commit()
        app.test_trade_id = trade.id

    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _auth(app):
    return {"Authorization": f"Bearer {app.test_token}"}


def _mocked_sell(app, on_chain_balance_tokens=1000.0, send_error=None):
    """Context manager stack mocking every external call sell_token makes."""
    price = {"usdPrice": 1.0, "sol_price_usd": 150.0, "name": "T", "symbol": "T"}

    requests_mock = MagicMock()
    requests_mock.get.return_value = _response(200, {"outAmount": "1000000", "outputMintDecimals": 6})
    requests_mock.post.return_value = _response(
        200, {"swapTransaction": base64.b64encode(b"tx").decode()}
    )

    solana_mock = MagicMock()
    solana_mock.get_token_supply.return_value = SimpleNamespace(value=SimpleNamespace(decimals=6))
    if send_error:
        solana_mock.send_transaction.side_effect = RuntimeError(send_error)
    else:
        solana_mock.send_transaction.return_value = SimpleNamespace(value="sig1")

    raw_balance = int(on_chain_balance_tokens * 10 ** 6)

    return [
        patch.object(manual_sell, "get_token_price", return_value=price),
        patch.object(manual_sell, "solana_client", solana_mock),
        patch.object(manual_sell, "requests", requests_mock),
        patch.object(manual_sell, "VersionedTransaction", _FakeVersionedTx),
        patch.object(manual_sell, "get_wallet_token_balance", return_value=(raw_balance, 6)),
    ]


def _apply(mocks):
    for m in mocks:
        m.start()
    return mocks


def _stop(mocks):
    for m in mocks:
        m.stop()


def test_manual_sell_writes_sent_status_for_reconciliation(client, app):
    """Regression: previously wrote no status at all (defaulting to
    "confirmed"), so a dropped/failed manual sell was never reconciled."""
    mocks = _apply(_mocked_sell(app))
    try:
        resp = client.put(
            f"/sell_token/{app.test_trade_id}/", json={"sell_percent": 10},
            headers=_auth(app),
        )
        assert resp.status_code == 200
        with app.app_context():
            history = TradeHistory.query.filter_by(trade_id=app.test_trade_id).first()
            assert history.status == "sent"
            assert history.trade_type == "SELL"  # not "ManualSEll" - must match the reconcile job's filter
    finally:
        _stop(mocks)


def test_manual_sell_caps_to_real_on_chain_balance(client, app):
    """Regression: previously trusted purchased_token_amount (1000 in the
    fixture) even when the wallet actually holds less on-chain."""
    # DB says 1000 available; chain says only 40 actually sitting in the wallet.
    mocks = _apply(_mocked_sell(app, on_chain_balance_tokens=40.0))
    try:
        resp = client.put(
            f"/sell_token/{app.test_trade_id}/", json={"sell_percent": 50},  # would be 500 if trusting the DB
            headers=_auth(app),
        )
        assert resp.status_code == 200
        with app.app_context():
            history = TradeHistory.query.filter_by(trade_id=app.test_trade_id).first()
            assert history.amount == pytest.approx(40.0)
    finally:
        _stop(mocks)


def test_manual_sell_rejects_when_wallet_holds_nothing_on_chain(client, app):
    mocks = _apply(_mocked_sell(app, on_chain_balance_tokens=0.0))
    try:
        resp = client.put(
            f"/sell_token/{app.test_trade_id}/", json={"sell_percent": 10},
            headers=_auth(app),
        )
        assert resp.status_code == 400
        assert "0 of this token" in resp.get_json()["message"]
    finally:
        _stop(mocks)


def test_manual_sell_handles_64_byte_private_key(client, app, tmp_path):
    """Regression: from_seed() alone raises for a 64-byte raw keypair, which
    attach_wallet/recover_wallet can legitimately produce."""
    kp = Keypair()
    key_file = tmp_path / "wallet64.key"
    key_file.write_bytes(bytes(kp))  # 64 bytes
    assert len(key_file.read_bytes()) == 64

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=app.test_user_id).first()
        wallet.private_key = str(key_file)
        wallet.public_key = str(kp.pubkey())
        trade = Trade.query.get(app.test_trade_id)
        trade.to_pubkey = str(kp.pubkey())
        db.session.commit()

    _FakeVersionedTx.wallet_pubkey = kp.pubkey()
    mocks = _apply(_mocked_sell(app))
    try:
        resp = client.put(
            f"/sell_token/{app.test_trade_id}/", json={"sell_percent": 10},
            headers=_auth(app),
        )
        assert resp.status_code == 200, resp.get_json()
    finally:
        _stop(mocks)
        _FakeVersionedTx.wallet_pubkey = Keypair.from_seed(b"\x00" * 32).pubkey()  # reset, unused after


def test_manual_sell_blocked_while_lock_held_by_another_path(client, app):
    """Regression: no lock meant a manual sell could race the auto-sell
    bot's cycle/instant path into a double sell on the same position."""
    lock = manual_sell._trade_lock(app.test_trade_id)
    lock.acquire()
    try:
        resp = client.put(
            f"/sell_token/{app.test_trade_id}/", json={"sell_percent": 10},
            headers=_auth(app),
        )
        assert resp.status_code == 409
    finally:
        lock.release()


def test_manual_sell_requires_auth(client, app):
    resp = client.put(f"/sell_token/{app.test_trade_id}/", json={"sell_percent": 10})
    assert resp.status_code == 401
