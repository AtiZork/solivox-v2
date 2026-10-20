"""Regression tests for AutoSnipeConfig range validation (B7).

Before this, _validate_config_payload only checked that fields were numeric,
not that they were sane: min_txns=0 (every new token instantly qualifies),
negative/huge buy_amount, slippage=1000 (10000 bps - accepts any output
amount), and out-of-range sell-side percentages (drop_cutoff > 100, etc.)
were all silently accepted and saved.
"""
import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from models import db, User


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["JWT_SECRET_KEY"] = "test-secret-key-at-least-32-bytes-long"
    db.init_app(app)
    JWTManager(app)

    import autosnipe
    app.register_blueprint(autosnipe.autosnipe_bp)

    with app.app_context():
        db.create_all()
        user = User(username="validation_tester", email="v@test.local")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        token = create_access_token(identity=str(user.id))
    app.test_token = token
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _auth(app):
    return {"Authorization": f"Bearer {app.test_token}"}


@pytest.mark.parametrize("payload, bad_field", [
    ({"min_txns": 0}, "min_txns"),
    ({"slippage": 1000}, "slippage"),
    ({"buy_amount": -1}, "buy_amount"),
    ({"buy_amount": 0}, "buy_amount"),
    ({"drop_cutoff": 150}, "drop_cutoff"),
    ({"drop_cutoff": -10}, "drop_cutoff"),
    ({"launch_delay": -5}, "launch_delay"),
    ({"priority_fee": -0.01}, "priority_fee"),
    ({"sell_priority_fee": -0.01}, "sell_priority_fee"),
    ({"sell_priority_fee": 5}, "sell_priority_fee"),
    ({"sell_at_100": 200}, "sell_at_100"),
])
def test_out_of_range_config_value_rejected(client, app, payload, bad_field):
    resp = client.post("/api/autosnipe", json=payload, headers=_auth(app))
    assert resp.status_code == 400
    assert any(bad_field in e for e in resp.get_json()["errors"])


def test_in_range_config_is_accepted(client, app):
    resp = client.post(
        "/api/autosnipe",
        json={"min_txns": 5, "slippage": 20, "buy_amount": 0.5, "drop_cutoff": 30},
        headers=_auth(app),
    )
    assert resp.status_code == 200


def test_slippage_of_100_percent_is_allowed(client, app):
    """Explicit client requirement: the strategy must be able to configure
    up to 100% slippage - do not cap this lower (it was briefly capped at
    50 during a hardening pass and had to be reverted)."""
    resp = client.post("/api/autosnipe", json={"slippage": 100}, headers=_auth(app))
    assert resp.status_code == 200


def test_slippage_above_100_percent_still_rejected(client, app):
    resp = client.post("/api/autosnipe", json={"slippage": 100.01}, headers=_auth(app))
    assert resp.status_code == 400


def test_sell_priority_fee_round_trips_through_list_endpoint(client, app):
    """Regression (C4): confirms the field is both saved and actually
    returned back, not just silently accepted and dropped."""
    create = client.post("/api/autosnipe", json={"sell_priority_fee": 0.03}, headers=_auth(app))
    assert create.status_code == 200
    config_id = create.get_json()["id"]

    listed = client.get("/api/autosnipe/list", headers=_auth(app)).get_json()
    saved = next(c for c in listed if c["id"] == config_id)
    assert saved["sell_priority_fee"] == pytest.approx(0.03)


def test_update_also_enforces_range(client, app):
    create = client.post("/api/autosnipe", json={}, headers=_auth(app))
    config_id = create.get_json()["id"]
    resp = client.put(
        f"/api/autosnipe/{config_id}", json={"slippage": 1000}, headers=_auth(app)
    )
    assert resp.status_code == 400
