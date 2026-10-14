"""
5-second inactivity rule: a position whose token has had no detected trade
for SNIPER_SELL_INACTIVITY_SEC is sold in full, independent of price.

Covers both halves of the feature:
  - check_inactivity(): the cheap, frequent sweep that decides which mints
    are overdue (pure in-memory timestamp comparisons, no RPC).
  - _run_inactivity_check(): what actually happens once a mint is judged
    overdue (price fetch, forced full-exit sell, going through the exact
    same locked/safe path as every other trigger).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import autosnipe_sell_script as sell_script
from test_sniper_take_profit import _live_config_trade, harness  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_inactivity_state():
    sell_script._last_activity.clear()
    sell_script._stream_was_connected = False
    yield
    sell_script._last_activity.clear()
    sell_script._stream_was_connected = False


# --- check_inactivity(): deciding what's overdue ---------------------------


def _connected_stream(connected=True):
    stream = MagicMock()
    stream.connected = connected
    return stream


def test_newly_seen_mint_is_seeded_not_fired(harness):
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    with patch.object(
        sell_script, "_open_trades_inactivity_settings", return_value={"MintNew": [(True, 5.0)]}
    ), patch.object(sell_script, "_run_inactivity_check") as run_check:
        sell_script.check_inactivity(harness.app)

    run_check.assert_not_called()
    assert "MintNew" in sell_script._last_activity


def test_mint_under_threshold_does_not_fire(harness):
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    sell_script._last_activity["MintQuiet"] = time.monotonic() - 2  # < 5s default
    with patch.object(
        sell_script, "_open_trades_inactivity_settings", return_value={"MintQuiet": [(True, 5.0)]}
    ), patch.object(sell_script, "_run_inactivity_check") as run_check:
        sell_script.check_inactivity(harness.app)

    run_check.assert_not_called()


def test_mint_past_threshold_fires(harness):
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    sell_script._last_activity["MintDead"] = time.monotonic() - 6  # > 5s default
    with patch.object(
        sell_script, "_open_trades_inactivity_settings", return_value={"MintDead": [(True, 5.0)]}
    ), patch.object(sell_script._instant_pool, "submit") as submit:
        sell_script.check_inactivity(harness.app)

    submit.assert_called_once()
    args = submit.call_args.args
    assert args[0] is sell_script._run_inactivity_check
    assert args[2] == "MintDead"
    assert args[3] >= 6


def test_firing_resets_timer_so_it_does_not_refire_every_tick(harness):
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    sell_script._last_activity["MintDead"] = time.monotonic() - 6
    with patch.object(
        sell_script, "_open_trades_inactivity_settings", return_value={"MintDead": [(True, 5.0)]}
    ), patch.object(sell_script._instant_pool, "submit") as submit:
        sell_script.check_inactivity(harness.app)
        submit.reset_mock()
        sell_script.check_inactivity(harness.app)  # immediately again, no real time passed

    submit.assert_not_called()


def test_disconnected_stream_suppresses_firing_even_if_overdue(harness):
    """No activity feed = no evidence of silence, only evidence we're not
    receiving anything - must not be treated as proof the token went quiet."""
    sell_script._sell_stream = _connected_stream(connected=False)
    sell_script._stream_was_connected = False
    sell_script._last_activity["MintDead"] = time.monotonic() - 100
    with patch.object(
        sell_script, "_open_trades_inactivity_settings", return_value={"MintDead": [(True, 5.0)]}
    ), patch.object(sell_script._instant_pool, "submit") as submit:
        sell_script.check_inactivity(harness.app)

    submit.assert_not_called()


def test_noop_when_feature_disabled(harness):
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    with patch.object(sell_script, "SNIPER_SELL_INACTIVITY_ENABLED", False), patch.object(
        sell_script, "_open_trades_inactivity_settings"
    ) as open_settings:
        sell_script.check_inactivity(harness.app)

    open_settings.assert_not_called()


def test_noop_when_sell_stream_never_configured(harness):
    sell_script._sell_stream = None
    with patch.object(sell_script, "_open_trades_inactivity_settings") as open_settings:
        sell_script.check_inactivity(harness.app)

    open_settings.assert_not_called()


def test_reconnect_resets_all_tracked_timers_with_no_immediate_refire(harness):
    stream = _connected_stream(connected=True)
    sell_script._sell_stream = stream
    sell_script._stream_was_connected = False  # was down, now back up: a reconnect
    sell_script._last_activity["MintA"] = time.monotonic() - 999
    sell_script._last_activity["MintB"] = time.monotonic() - 999

    with patch.object(
        sell_script, "_open_trades_inactivity_settings",
        return_value={"MintA": [(True, 5.0)], "MintB": [(True, 5.0)]},
    ), patch.object(sell_script._instant_pool, "submit") as submit:
        sell_script.check_inactivity(harness.app)

    submit.assert_not_called()  # fresh grace period, not instantly overdue
    assert sell_script._stream_was_connected is True


def test_mint_level_prefilter_skips_only_trades_that_want_it(harness):
    """
    Per-trade thresholds: two trades share a mint, one wants 5s and one wants
    15s. At 6s elapsed, the mint-level sweep must still fire (the 5s trade
    could be due) - the per-trade filtering happens in _run_inactivity_check,
    tested separately below.
    """
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    sell_script._last_activity["MintMixed"] = time.monotonic() - 6
    with patch.object(
        sell_script, "_open_trades_inactivity_settings",
        return_value={"MintMixed": [(True, 5.0), (True, 15.0)]},
    ), patch.object(sell_script._instant_pool, "submit") as submit:
        sell_script.check_inactivity(harness.app)

    submit.assert_called_once()


def test_mint_level_prefilter_does_not_fire_if_no_trade_is_due_yet(harness):
    """Both trades on the mint want 15s; at 6s elapsed, neither is due, so
    the mint itself must not be queued at all."""
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    sell_script._last_activity["MintPatient"] = time.monotonic() - 6
    with patch.object(
        sell_script, "_open_trades_inactivity_settings",
        return_value={"MintPatient": [(True, 15.0), (True, 15.0)]},
    ), patch.object(sell_script._instant_pool, "submit") as submit:
        sell_script.check_inactivity(harness.app)

    submit.assert_not_called()


def test_mint_level_prefilter_ignores_disabled_trades(harness):
    """A mint whose only open trade has the rule disabled must never fire,
    no matter how long it's been quiet."""
    sell_script._sell_stream = _connected_stream()
    sell_script._stream_was_connected = True
    sell_script._last_activity["MintDisabled"] = time.monotonic() - 999
    with patch.object(
        sell_script, "_open_trades_inactivity_settings",
        return_value={"MintDisabled": [(False, 5.0)]},
    ), patch.object(sell_script._instant_pool, "submit") as submit:
        sell_script.check_inactivity(harness.app)

    submit.assert_not_called()


def test_inactivity_sweep_reconnect_log_does_not_crash_the_scheduler_job():
    """
    Regression: check_inactivity used to call logger.info() - the module
    logger, which has a DB-writing handler (DBLogHandler) - without an app
    context pushed. That crashed the scheduler job with "Working outside of
    application context" on literally every tick, so the inactivity rule
    never actually ran at all, silently, against a live 25-position backlog.

    This exercises the REAL logger/DB-handler path (not the shared harness's
    fully-mocked logger, which hides exactly this class of bug) to make sure
    it can't silently regress again.

    Note: pytest's own logging setup leaves this logger's *effective* level
    at WARNING by default, which would silently swallow the info() call
    before it ever reaches the handler - masking this exact bug without
    actually exercising the app-context path at all. logger.setLevel(INFO)
    below restores the level the app itself sets in production
    (logging.basicConfig(level=logging.INFO) at import time) so this test
    verifies what it claims to.
    """
    import logging

    from flask import Flask
    from models import db as real_db

    sell_script.logger.setLevel(logging.INFO)

    app = Flask("inactivity-context-regression-test")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    real_db.init_app(app)
    with app.app_context():
        real_db.create_all()  # so the real TradeLog insert below has a table to write to

    stream = MagicMock()
    stream.connected = True
    sell_script._sell_stream = stream
    sell_script._stream_was_connected = False  # forces the reconnect-log branch

    # Deliberately NOT mocking db.session.add/commit: that's the exact call
    # whose app-context resolution is what this test is verifying.
    with patch.object(sell_script, "_open_trade_mints", return_value=set()):
        sell_script.check_inactivity(app)  # must not raise


def test_on_token_activity_updates_last_activity_timestamp(harness):
    before = time.monotonic()
    with patch.object(sell_script, "_instant_pool") as pool:
        sell_script.on_token_activity(harness.app, "MintLive")
    assert sell_script._last_activity["MintLive"] >= before


# --- _run_inactivity_check(): what happens once a mint fires ---------------


def test_inactivity_trigger_sells_full_remaining_position(harness):
    trade = _live_config_trade(id=40, token_address="MintDead", purchased_token_amount=1000.0)
    harness.trades = [trade]
    harness.prices = {"MintDead": 1.2}  # flat - no TP/SL would fire on its own

    sell_script._run_inactivity_check(harness.app, "MintDead", 5.3)

    assert harness.sent == 1
    assert trade.executed is True
    assert trade.purchased_token_amount == 0
    assert "no activity for 5.3s" in _logged(harness, "info")


def test_inactivity_trigger_respects_per_trade_disabled_flag(harness):
    """Sniper-config-level override: a trade bought with the rule disabled
    must never be force-sold by it, no matter how long its token is quiet."""
    trade = _live_config_trade(
        id=45, token_address="MintDisabled", purchased_token_amount=1000.0, inactivity_sell_enabled=False,
    )
    harness.trades = [trade]
    harness.prices = {"MintDisabled": 1.2}

    sell_script._run_inactivity_check(harness.app, "MintDisabled", 50.0)  # way past any threshold

    assert harness.sent == 0
    assert trade.executed is False


def test_inactivity_trigger_respects_per_trade_custom_threshold(harness):
    """A trade configured for 15s must not fire at 6s elapsed (even though
    the global/other-trade default is 5s), but must fire once 15s have passed."""
    trade = _live_config_trade(
        id=46, token_address="MintPatient", purchased_token_amount=1000.0, inactivity_sell_seconds=15.0,
    )
    harness.trades = [trade]
    harness.prices = {"MintPatient": 1.2}

    sell_script._run_inactivity_check(harness.app, "MintPatient", 6.0)
    assert harness.sent == 0

    sell_script._run_inactivity_check(harness.app, "MintPatient", 15.2)
    assert harness.sent == 1
    assert trade.executed is True


def test_inactivity_trigger_mixed_thresholds_on_shared_mint_fire_independently(harness):
    """Two trades on the same mint, different configs: at 6s elapsed only
    the 5s-threshold trade sells; the 15s one waits."""
    fast = _live_config_trade(id=47, token_address="MintMixed", purchased_token_amount=500.0, inactivity_sell_seconds=5.0)
    slow = _live_config_trade(id=48, token_address="MintMixed", purchased_token_amount=700.0, inactivity_sell_seconds=15.0)
    harness.trades = [fast, slow]
    harness.prices = {"MintMixed": 1.2}

    sell_script._run_inactivity_check(harness.app, "MintMixed", 6.0)

    assert harness.sent == 1
    assert fast.executed is True
    assert slow.executed is False


def test_inactivity_trigger_missing_fields_fall_back_to_global_default(harness):
    """A trade from before this field existed (None on both) behaves exactly
    like the previous global-only implementation: enabled, 5s threshold."""
    trade = _live_config_trade(
        id=49, token_address="MintLegacy", purchased_token_amount=1000.0,
        inactivity_sell_enabled=None, inactivity_sell_seconds=None,
    )
    harness.trades = [trade]
    harness.prices = {"MintLegacy": 1.2}

    sell_script._run_inactivity_check(harness.app, "MintLegacy", 5.1)

    assert harness.sent == 1
    assert trade.executed is True


def test_inactivity_trigger_does_not_touch_take_profit_tier_bookkeeping(harness):
    """A full exit via inactivity isn't a take-profit sale - tp_pct_sold /
    last_tp_tier must stay whatever they already were."""
    trade = _live_config_trade(id=41, token_address="MintDead", purchased_token_amount=1000.0, tp_pct_sold=10, last_tp_tier=2.0)
    harness.trades = [trade]
    harness.prices = {"MintDead": 1.2}

    sell_script._run_inactivity_check(harness.app, "MintDead", 5.0)

    assert trade.tp_pct_sold == 10  # unchanged
    assert trade.last_tp_tier == 2.0  # unchanged


def test_inactivity_trigger_skipped_if_another_path_holds_the_lock(harness):
    trade = _live_config_trade(id=42, token_address="MintDead", purchased_token_amount=1000.0)
    harness.trades = [trade]
    harness.prices = {"MintDead": 1.2}

    lock = sell_script._trade_lock(42)
    lock.acquire()
    try:
        sell_script._run_inactivity_check(harness.app, "MintDead", 5.0)
        assert harness.sent == 0
    finally:
        lock.release()


def test_inactivity_price_fetch_failure_is_logged_not_raised(harness):
    trade = _live_config_trade(id=44, token_address="MintDead")
    harness.trades = [trade]
    with patch.object(sell_script, "get_token_price", side_effect=RuntimeError("shyft down")):
        sell_script._run_inactivity_check(harness.app, "MintDead", 5.0)  # must not raise
    assert "price fetch failed" in _logged(harness, "warning")
    assert harness.sent == 0


def test_inactivity_check_with_no_open_trades_for_mint_is_noop(harness):
    harness.trades = []
    sell_script._run_inactivity_check(harness.app, "MintGone", 5.0)
    assert harness.sent == 0


def test_inactivity_and_take_profit_racing_sells_exactly_once(harness):
    """Both triggers can plausibly fire around the same moment - the shared
    trade lock must still guarantee exactly one sale."""
    trade = _live_config_trade(id=43, token_address="MintTP")
    harness.trades = [trade]
    harness.prices = {"MintTP": 2.33}  # genuinely crosses the 100% take-profit tier

    import threading
    workers = [
        threading.Thread(target=harness.run_cycle),
        threading.Thread(target=sell_script._run_inactivity_check, args=(harness.app, "MintTP", 5.0)),
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert harness.sent == 1


def _logged(h, level):
    return " ".join(str(c.args[0]) for c in getattr(h.logger, level).call_args_list)
