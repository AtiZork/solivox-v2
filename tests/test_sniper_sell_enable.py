"""Unit tests for sniper sell enable/disable gates."""

from __future__ import annotations

from types import SimpleNamespace

from autosnipe_sell_logic import evaluate_autosnipe_sell_amount


def _trade(**kwargs):
    base = dict(
        id=1,
        purchased_token_amount=1000.0,
        initial_price=1.0,
        drop_cutoff=30,
        drop_cutoff_enabled=True,
        # Keep ceiling out of the way so FIELD_1/2/3 can be tested in isolation
        drop_until_profit=100000,
        drop_after_100=50,
        drop_after_100_enabled=True,
        drop_after_400=30,
        drop_after_400_enabled=True,
        sell_at_200=0,
        sell_at_400=0,
        sell_at_1000=0,
        sell_at_1500=0,
        sell_at_2500=0,
        sell_at_4000=0,
        sell_at_10000=0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_drop_cutoff_enabled_sells_on_buy_price_drop():
    trade = _trade()
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=0.69)
    assert amount == 1000.0
    assert "Drops below" in message


def test_drop_cutoff_disabled_skips_buy_price_drop():
    trade = _trade(drop_cutoff_enabled=False)
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=0.69)
    assert amount == 0
    assert message is None or "Drops below" not in message


def test_drop_after_100_enabled_sells_on_peak_drop():
    trade = _trade()
    tracking = {1: 3.0}  # peak after >=100% profit
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=1.4, price_tracking_map=tracking)
    # profit_multiplier=1.4 < 2.0 so trailing branch not entered; use 2.2
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=2.2, price_tracking_map=tracking)
    # drop = 100 * (3.0 - 2.2) / 3.0 ~= 26.7 < 50 → no sell
    tracking = {1: 4.0}
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=1.8, price_tracking_map=tracking)
    # drop = 55% >= 50, profit_multiplier=1.8 < 2 → not in trailing branch
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=2.0, price_tracking_map={1: 5.0})
    # drop = 60% >= 50, profit_multiplier = 2.0
    assert amount == 1000.0
    assert "after 100% profit" in message


def test_drop_after_100_disabled_does_not_sell():
    trade = _trade(drop_after_100_enabled=False, drop_after_400_enabled=False)
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=2.0, price_tracking_map={1: 5.0})
    assert amount == 0
    assert message is None


def test_drop_after_400_enabled_sells_when_eligible():
    trade = _trade(drop_after_100=90)  # keep FIELD_1 from matching first
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=5.5, price_tracking_map={1: 8.0})
    # drop ~= 31.25 >= 30, profit_multiplier = 5.5 >= 5
    assert amount == 1000.0
    assert "after 400% profit" in message


def test_drop_after_400_disabled_skips():
    trade = _trade(drop_after_100=90, drop_after_400_enabled=False)
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=5.5, price_tracking_map={1: 8.0})
    assert amount == 0
    assert message is None


def test_missing_enable_attrs_default_to_on():
    trade = SimpleNamespace(
        id=2,
        purchased_token_amount=100.0,
        initial_price=1.0,
        drop_cutoff=30,
        drop_until_profit=100000,
        drop_after_100=50,
        drop_after_400=30,
        sell_at_200=0,
        sell_at_400=0,
        sell_at_1000=0,
        sell_at_1500=0,
        sell_at_2500=0,
        sell_at_4000=0,
        sell_at_10000=0,
    )
    amount, message = evaluate_autosnipe_sell_amount(trade, current_price=0.5)
    assert amount == 100.0
    assert "Drops below" in message
