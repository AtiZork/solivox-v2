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


def test_buy_txns_max_usd_is_mandatory_when_setting_the_min(client, app):
    """Client requirement (reversed from the original optional design): a
    save that sets the min floor but omits the max ceiling must be rejected,
    not silently accepted as "no limit"."""
    resp = client.post("/api/autosnipe", json={"buy_txns_over_80_usd": 80}, headers=_auth(app))
    assert resp.status_code == 400
    assert any("buy_txns_max_usd" in e for e in resp.get_json()["errors"])


def test_save_not_touching_buy_amount_range_is_unaffected(client, app):
    """The mandatory check only applies when the buy-amount range is
    actually being configured - an unrelated sell-side-only save must not
    be blocked by a field it never touches."""
    resp = client.post("/api/autosnipe", json={"drop_cutoff": 25}, headers=_auth(app))
    assert resp.status_code == 200


def test_buy_txns_max_usd_below_min_rejected(client, app):
    resp = client.post(
        "/api/autosnipe",
        json={"buy_txns_over_80_usd": 100, "buy_txns_max_usd": 50},
        headers=_auth(app),
    )
    assert resp.status_code == 400
    assert any("buy_txns_max_usd" in e for e in resp.get_json()["errors"])


def test_buy_txns_max_usd_out_of_range_rejected(client, app):
    resp = client.post("/api/autosnipe", json={"buy_txns_max_usd": -5}, headers=_auth(app))
    assert resp.status_code == 400
    assert any("buy_txns_max_usd" in e for e in resp.get_json()["errors"])


def test_buy_txns_max_usd_round_trips_through_list_endpoint(client, app):
    """Client's two example strategies - confirms both are just plain config
    saves once the ceiling field exists, nothing extra needed."""
    create = client.post(
        "/api/autosnipe",
        json={"min_txns": 10, "buy_txns_over_80_usd": 100, "buy_txns_max_usd": 200},
        headers=_auth(app),
    )
    assert create.status_code == 200
    config_id = create.get_json()["id"]

    listed = client.get("/api/autosnipe/list", headers=_auth(app)).get_json()
    saved = next(c for c in listed if c["id"] == config_id)
    assert saved["buy_txns_max_usd"] == pytest.approx(200)
    assert saved["buy_txns_over_80_usd"] == 100
    assert saved["min_txns"] == 10


def test_buy_txns_max_usd_cannot_be_cleared_back_to_unbounded(client, app):
    """Once mandatory, an update explicitly trying to blank it back out
    must be rejected - "no limit" is no longer a valid configured state
    going forward, even though pre-existing untouched rows may still have
    it (see models.py AutoSnipeConfig.buy_txns_max_usd)."""
    create = client.post(
        "/api/autosnipe", json={"buy_txns_over_80_usd": 100, "buy_txns_max_usd": 200},
        headers=_auth(app),
    )
    config_id = create.get_json()["id"]
    resp = client.put(
        f"/api/autosnipe/{config_id}", json={"buy_txns_max_usd": None}, headers=_auth(app)
    )
    assert resp.status_code == 400


def test_update_rejects_max_below_min_when_both_given(client, app):
    create = client.post(
        "/api/autosnipe",
        json={"buy_txns_over_80_usd": 100, "buy_txns_max_usd": 300},
        headers=_auth(app),
    )
    config_id = create.get_json()["id"]
    resp = client.put(
        f"/api/autosnipe/{config_id}",
        json={"buy_txns_over_80_usd": 100, "buy_txns_max_usd": 20},
        headers=_auth(app),
    )
    assert resp.status_code == 400
