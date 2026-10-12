"""
Sells shortly after a buy, cooldown reasons, and the sell latency line.

Before the fix the wallet balance was read at finalized (~30 slots / ~12s
behind). A stop loss or take profit firing in that window read 0 tokens, was
marked "empty" and backed off for SNIPER_SELL_EMPTY_COOLDOWN_SEC (60s).
"""

from __future__ import annotations

import time
from unittest.mock import patch

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
    (lambda h: setattr(h.db.session.commit, "side_effect", RuntimeError("db down")),
     "empty", "db_update_failed_after_send"),
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
