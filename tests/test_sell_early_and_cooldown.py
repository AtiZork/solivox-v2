"""
Sells shortly after a buy, cooldown reasons, and the sell latency line.

Before the fix the wallet balance was read at finalized (~30 slots / ~12s
behind). A stop loss or take profit firing in that window read 0 tokens, was
marked "empty" and backed off for SNIPER_SELL_EMPTY_COOLDOWN_SEC (60s).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from solana.rpc.commitment import Confirmed

import autosnipe_sell_script as sell_script
from test_sniper_take_profit import _live_config_trade, _quoted_amounts, harness  # noqa: F401


@pytest.fixture
def latency():
    with patch("latency_log.logger") as log:
        yield log


def _lines(latency_logger):
    return [c.args[0] for c in latency_logger.info.call_args_list]


def test_sell_preflights_at_confirmed(harness):
    sl = _live_config_trade(id=1, token_address="MintSL")
    harness.trades = [sl]
    harness.prices = {"MintSL": 0.5}

    harness.run_cycle()

    assert harness.sent == 1
    assert harness.send_opts[0].preflight_commitment == Confirmed
    assert harness.send_opts[0].skip_confirmation is True


def test_stop_loss_before_buy_lands_retries_shortly_then_sells(harness, latency):
    sl = _live_config_trade(id=21, token_address="MintEarly")
    harness.trades = [sl]
    harness.prices = {"MintEarly": 0.5}
    harness.balances = {"MintEarly": 0}  # buy not landed yet

    with patch.object(sell_script, "SNIPER_SELL_EMPTY_GRACE_SEC", 15):
        harness.run_cycle()
        wait = sell_script._retry_after[21] - time.monotonic()
        assert 0 < wait <= sell_script.SNIPER_SELL_RETRY_COOLDOWN_SEC  # short, not 60s
        assert "reason=zero_balance_in_grace" in _lines(latency)[-1]

        harness.balances = {"MintEarly": 5_000_000_000}  # buy landed
        sell_script._retry_after[21] = 0  # short cooldown elapsed
        harness.run_cycle()

    assert harness.sent == 1
    assert sl.executed is True
    assert _quoted_amounts(harness) == [5_000_000_000]
    assert 21 not in sell_script._empty_since


def test_still_empty_after_grace_backs_off_long(harness, latency):
    gone = _live_config_trade(id=22, token_address="MintGone")
    harness.trades = [gone]
    harness.prices = {"MintGone": 0.5}
    harness.balances = {"MintGone": 0}

    with patch.object(sell_script, "SNIPER_SELL_EMPTY_GRACE_SEC", 15):
        harness.run_cycle()
        assert sell_script._retry_after[22] - time.monotonic() <= sell_script.SNIPER_SELL_RETRY_COOLDOWN_SEC
        sell_script._empty_since[22] -= 16  # first seen empty 16s ago
        sell_script._retry_after[22] = 0
        harness.run_cycle()

    assert sell_script._retry_after[22] > time.monotonic() + 50
    assert harness.requests.get.call_count == 0  # never quoted a sale of nothing
    last = _lines(latency)[-1]
    assert "outcome=empty" in last and "reason=zero_balance" in last and "cooldown_sec=60" in last


@pytest.mark.parametrize("setup, outcome, reason", [
    (lambda h: setattr(h, "send_error", "Blockhash not found"), "failed", "send_rejected"),
    (lambda h: h.quote_status.update({"MintR": 400}), "empty", "quote_NO_ROUTES_FOUND"),
    (lambda h: h.balances.update({"MintR": RuntimeError("rpc down")}), "failed", "balance_read_failed"),
    # A DB that can't even commit the pre-send sell_pending marker (action-
    # plan 1.4b) is refused a send entirely, rather than sending and only
    # then discovering it can't be recorded - see test_sniper_take_profit's
    # test_persistent_db_failure_prevents_any_send for the full scenario.
    (lambda h: setattr(h.db.session.commit, "side_effect", RuntimeError("db down")),
     "failed", "sell_pending_marker_failed"),
])
def test_cooldown_reason_is_logged(harness, latency, setup, outcome, reason):
    trade = _live_config_trade(id=23, token_address="MintR")
    harness.trades = [trade]
    harness.prices = {"MintR": 0.5}
    setup(harness)

    harness.run_cycle()

    line = _lines(latency)[-1]
    assert f"outcome={outcome}" in line, line
    assert f"reason={reason}" in line, line
    assert "cooldown_sec=" in line


def test_rate_limited_quote_backs_off_longer_than_a_generic_failure(harness, latency):
    """
    Regression: a Jupiter 429 used to retry on the same short cooldown as any
    other failure. Under a backlog of many simultaneously-due sells, that just
    kept re-triggering 429s instead of letting the backlog drain.
    """
    trade = _live_config_trade(id=28, token_address="MintRL")
    harness.trades = [trade]
    harness.prices = {"MintRL": 0.5}

    def _rate_limited(trade_data, current_price, trigger, timer=None, force_reason=None):
        timer.reason = "quote_http_429"
        return "failed"

    with patch.object(sell_script, "_sell_if_due", side_effect=_rate_limited):
        harness.run_cycle()

    wait = sell_script._retry_after[28] - time.monotonic()
    assert sell_script.SNIPER_SELL_RETRY_COOLDOWN_SEC < wait <= sell_script.SNIPER_SELL_RATE_LIMIT_COOLDOWN_SEC
    assert "cooldown_sec=20.0" in _lines(latency)[-1]


def test_rate_limited_swap_also_gets_the_longer_cooldown(harness, latency):
    trade = _live_config_trade(id=29, token_address="MintRL2")
    harness.trades = [trade]
    harness.prices = {"MintRL2": 0.5}

    def _rate_limited(trade_data, current_price, trigger, timer=None, force_reason=None):
        timer.reason = "swap_http_429"
        return "failed"

    with patch.object(sell_script, "_sell_if_due", side_effect=_rate_limited):
        harness.run_cycle()

    wait = sell_script._retry_after[29] - time.monotonic()
    assert wait > sell_script.SNIPER_SELL_RETRY_COOLDOWN_SEC


def test_non_json_429_quote_response_still_gets_tagged_and_backed_off(harness, latency):
    """
    Regression: the free Jupiter tier (lite-api.jup.ag, no API_KEY) returns a
    plain-text body for a 429 ("Rate limit exceeded..."), not JSON. Calling
    .json() on that raised before the status code was ever checked, so the
    429 fell through to the generic exception handler and got the short 2s
    cooldown instead of the dedicated rate-limit backoff - observed live
    during a burst of simultaneous inactivity-triggered sells.
    """
    trade = _live_config_trade(id=31, token_address="MintFreeTier")
    harness.trades = [trade]
    harness.prices = {"MintFreeTier": 0.5}

    def _non_json_429(url, params=None, **kwargs):
        resp = MagicMock()
        resp.status_code = 429
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        resp.text = "Rate limit exceeded, please try again later."
        return resp

    harness.requests.get.side_effect = _non_json_429

    harness.run_cycle()

    wait = sell_script._retry_after[31] - time.monotonic()
    assert sell_script.SNIPER_SELL_RETRY_COOLDOWN_SEC < wait <= sell_script.SNIPER_SELL_RATE_LIMIT_COOLDOWN_SEC
    line = _lines(latency)[-1]
    assert "outcome=failed" in line and "reason=quote_http_429" in line and "cooldown_sec=20.0" in line


def test_sold_line_has_stages_and_signature(harness, latency):
    tp = _live_config_trade(id=24, token_address="MintTP")
    harness.trades = [tp]
    harness.prices = {"MintTP": 2.33}

    harness.run_cycle()

    line = _lines(latency)[-1]
    assert line.startswith("[latency] path=sell trade=24 mint=MintTP trigger=cycle")
    assert "outcome=sold" in line
    for stage in ("price=", "queue_wait=", "db_refresh=", "balance=", "quote=", "swap=", "send=", "db=", "sig=sig1"):
        assert stage in line, stage


def test_instant_line_has_queue_wait_from_activity(harness, latency):
    tp = _live_config_trade(id=25, token_address="MintTP")
    harness.trades = [tp]
    harness.prices = {"MintTP": 2.33}

    sell_script.check_trades_for_mint(harness.app, "MintTP", time.monotonic() - 0.3)

    line = _lines(latency)[-1]
    assert "trigger=instant" in line and "outcome=sold" in line
    assert float(line.split("queue_wait=")[1].split()[0]) >= 290


def test_no_latency_line_when_no_rule_fires(harness, latency):
    flat = _live_config_trade(id=26, token_address="MintFlat")
    harness.trades = [flat]
    harness.prices = {"MintFlat": 1.2}

    harness.run_cycle()
    sell_script.check_trades_for_mint(harness.app, "MintFlat", time.monotonic())

    assert _lines(latency) == []


def test_no_latency_line_while_cooling(harness, latency):
    trade = _live_config_trade(id=27, token_address="MintCool")
    harness.trades = [trade]
    harness.prices = {"MintCool": 0.5}
    sell_script._retry_after[27] = time.monotonic() + 30

    harness.run_cycle()

    assert harness.sent == 0
    assert _lines(latency) == []


def test_full_exit_of_an_autosnipe_trade_frees_the_mint_for_a_rebuy(harness):
    """Action-plan 1.3: closing an AUTOSNIPE position out fully must clear
    the in-memory buy-dedup fast path (BOUGHT_TOKENS), not just the DB row's
    executed flag - otherwise a legitimate rebuy of this mint later in the
    same process run would stay blocked until a restart happened to clear
    it, even though the new open-positions-only dedup scope allows it."""
    trade = _live_config_trade(id=32, token_address="MintFullExit", auto_snipe=True, user_id=9)
    harness.trades = [trade]
    harness.prices = {"MintFullExit": 0.69}  # below the 30% stop-loss cutoff: sells 100%

    with patch.object(sell_script, "mark_autosnipe_position_closed") as mark_closed:
        harness.run_cycle()

    assert trade.executed is True
    mark_closed.assert_called_once_with(9, "MintFullExit")


def test_unusable_price_is_warned_about_once_then_deduped(harness):
    """
    Regression: a migrated token priced against a non-SOL/non-USDC quote
    asset (see shyft_pricing.get_pumpswap_pool_price) resolves to no usable
    price forever. Previously this failed completely silently; it must now
    warn, but not spam a warning every single cycle tick forever.
    """
    sell_script._no_price_warned_until.clear()
    trade = _live_config_trade(id=30, token_address="MintUnpriceable")
    harness.trades = [trade]
    harness.prices = {"MintUnpriceable": None}

    harness.run_cycle()
    first_count = harness.logger.warning.call_count
    assert any("No usable price" in str(c.args[0]) for c in harness.logger.warning.call_args_list)

    harness.run_cycle()  # immediately again: must not re-warn yet
    assert harness.logger.warning.call_count == first_count

    sell_script._no_price_warned_until[30] = 0  # simulate the dedup window elapsing
    harness.run_cycle()
    assert harness.logger.warning.call_count > first_count


# --- Critical-decision fail-safe (client request) ---------------------------


def test_failsafe_forces_a_retry_past_the_empty_cooldown_after_5s(harness):
    """A decision that's been firm for SNIPER_SELL_CRITICAL_FAILSAFE_SEC must
    not keep waiting out a much longer EMPTY/RETRY cooldown left over from an
    earlier, unrelated failed attempt."""
    trade = _live_config_trade(id=40, token_address="MintFailsafe")
    harness.trades = [trade]
    harness.prices = {"MintFailsafe": 0.5}  # triggers the stop-loss
    sell_script._retry_after[40] = time.monotonic() + 60  # a 60s EMPTY cooldown, still active
    sell_script._decision_pending_since[40] = time.monotonic() - 6  # decision has been firm 6s

    harness.run_cycle()

    assert harness.sent == 1  # forced through despite the cooldown not having expired


def test_failsafe_does_not_fire_before_the_threshold_elapses(harness):
    trade = _live_config_trade(id=41, token_address="MintTooSoon")
    harness.trades = [trade]
    harness.prices = {"MintTooSoon": 0.5}
    sell_script._retry_after[41] = time.monotonic() + 60
    sell_script._decision_pending_since[41] = time.monotonic() - 2  # only 2s so far

    harness.run_cycle()

    assert harness.sent == 0  # cooldown still respected - not overdue yet


def test_failsafe_never_overrides_an_active_rate_limit_cooldown(harness):
    """Retrying through an active 429 backoff defeats the entire reason
    SNIPER_SELL_RATE_LIMIT_COOLDOWN_SEC exists - the fail-safe must not touch
    this one even once it's well overdue."""
    trade = _live_config_trade(id=42, token_address="MintRateLimited")
    harness.trades = [trade]
    harness.prices = {"MintRateLimited": 0.5}
    sell_script._retry_after[42] = time.monotonic() + 20
    sell_script._rate_limited_until[42] = time.monotonic() + 20
    sell_script._decision_pending_since[42] = time.monotonic() - 10  # well past the 5s threshold

    harness.run_cycle()

    assert harness.sent == 0


def test_failsafe_tracking_resets_once_the_decision_resolves(harness):
    """Once nothing is left to sell (decision amount drops to 0), the
    pending-since timer must clear - otherwise a brand new, unrelated
    decision on this trade later would incorrectly look overdue immediately."""
    trade = _live_config_trade(id=43, token_address="MintResolved")
    harness.trades = [trade]
    harness.prices = {"MintResolved": 1.0}  # flat: no rule fires
    sell_script._decision_pending_since[43] = time.monotonic() - 10

    harness.run_cycle()

    assert 43 not in sell_script._decision_pending_since


def test_failsafe_tracking_clears_on_a_successful_sell(harness):
    trade = _live_config_trade(id=44, token_address="MintSoldOk")
    harness.trades = [trade]
    harness.prices = {"MintSoldOk": 0.5}

    harness.run_cycle()

    assert harness.sent == 1
    assert 44 not in sell_script._decision_pending_since
    assert 44 not in sell_script._rate_limited_until


def test_failsafe_is_per_config_disabled_trade_never_overrides_the_cooldown(harness):
    """Client correction: this must be a per-sniper-config enable/disable +
    adjustable value, not a fixed global behavior - a trade with it switched
    off must keep respecting cooldowns indefinitely, even once well overdue
    by the global default threshold."""
    trade = _live_config_trade(id=45, token_address="MintFailsafeOff", critical_failsafe_enabled=False)
    harness.trades = [trade]
    harness.prices = {"MintFailsafeOff": 0.5}
    sell_script._retry_after[45] = time.monotonic() + 60
    sell_script._decision_pending_since[45] = time.monotonic() - 100  # way overdue

    harness.run_cycle()

    assert harness.sent == 0  # never overridden - this config has it off


def test_failsafe_uses_the_per_trade_threshold_not_the_global_default(harness):
    """A config with a longer custom threshold (20s) must not fire the
    fail-safe at the 5s global default - only once its own value elapses."""
    trade = _live_config_trade(id=46, token_address="MintCustomThreshold", critical_failsafe_seconds=20.0)
    harness.trades = [trade]
    harness.prices = {"MintCustomThreshold": 0.5}
    sell_script._retry_after[46] = time.monotonic() + 60
    sell_script._decision_pending_since[46] = time.monotonic() - 10  # past the 5s default, not the 20s custom one

    harness.run_cycle()

    assert harness.sent == 0  # not yet overdue by this trade's own 20s threshold

    sell_script._decision_pending_since[46] = time.monotonic() - 25  # now past the custom threshold
    harness.run_cycle()

    assert harness.sent == 1
