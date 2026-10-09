"""
Regression tests for the sniper take-profit bug.

Root cause: evaluate_autosnipe_sell_amount returned "skip" whenever
profit_multiplier * 100 >= drop_until_profit. Every live trade uses
drop_until_profit=99, so any price >= 0.99x the buy price skipped all
take-profit and trailing-stop rules; only the stop loss (checked first) ever
sold. Config values below are the real values from the client's trade table.
"""

from __future__ import annotations

import base64
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from solders.keypair import Keypair

import autosnipe_sell_script as sell_script
from autosnipe_sell_logic import evaluate_autosnipe_sell


def _live_config_trade(**kwargs):
    """A Trade carrying the exact sell config every client trade has."""
    base = dict(
        id=1,
        token_address="Mint1111111111111111111111111111111111pump",
        to_pubkey="WalletPubkey",
        purchased_token_amount=1000.0,
        initial_price=1.0,
        executed=False,
        autosnipe_sell_slippage=100,
        default_gas_fee=0.01,
        drop_cutoff=30,
        drop_cutoff_enabled=True,
        drop_until_profit=99,
        drop_after_100=50,
        drop_after_100_enabled=True,
        drop_after_400=30,
        drop_after_400_enabled=True,
        sell_at_100=10,
        sell_at_200=10,
        sell_at_400=10,
        sell_at_1000=10,
        sell_at_1500=10,
        sell_at_2500=10,
        sell_at_4000=10,
        sell_at_10000=10,
        last_tp_tier=0,
        initial_token_amount=None,
        tp_pct_sold=0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


# --- Decision logic -------------------------------------------------------

@pytest.mark.parametrize("price", [1.0, 1.5, 1.99])
def test_below_100_percent_profit_does_not_sell(price):
    decision = evaluate_autosnipe_sell(_live_config_trade(), price, {})
    assert decision.amount == 0
    assert decision.message is None


def test_exactly_100_percent_profit_sells_configured_pct():
    decision = evaluate_autosnipe_sell(_live_config_trade(), 2.0, {})
    assert decision.amount == pytest.approx(100.0)  # 10% of 1000
    assert decision.message == "Auto-Sell 10% of position at 100% Profit (take-profit total 10%)"
    assert decision.take_profit_tier == 2.0
    assert decision.take_profit_pct == 10


def test_drop_until_profit_99_no_longer_blocks_take_profit():
    """The exact live config that produced "Profit reached limit of 99.0%"."""
    trade = _live_config_trade(drop_until_profit=99)
    decision = evaluate_autosnipe_sell(trade, 2.33, {})
    assert decision.amount > 0
    assert "Profit reached limit" not in (decision.message or "")


@pytest.mark.parametrize(
    "name, initial, multiplier, expected_pct, expected_label, expected_tier",
    [
        # Real open trades priced live from the client's trade table — each was
        # returning "Profit reached limit of 99.0%, skipping further sells."
        # Every tier reached sells its 10%, so a price that jumped past several
        # tiers sells all of them at once.
        ("KIRKDAY #322", 4.867203759355247e-06, 2.330, 10, "100%", 2.0),
        ("FortniteOG #87", 1.0775447067809357e-05, 3.713, 20, "200%", 3.0),
        ("Sicey #240", 5.527842308173664e-06, 6.945, 30, "400%", 5.0),
        ("kumo #311", 7.518609214447407e-06, 9.094, 30, "400%", 5.0),
    ],
)
def test_real_trades_above_100_percent_now_sell(name, initial, multiplier, expected_pct, expected_label, expected_tier):
    trade = _live_config_trade(initial_price=initial, purchased_token_amount=20000.0)
    decision = evaluate_autosnipe_sell(trade, initial * multiplier, {})
    assert decision.amount == pytest.approx(20000.0 * expected_pct / 100), name
    assert decision.message == (
        f"Auto-Sell {expected_pct}% of position at {expected_label} Profit "
        f"(take-profit total {expected_pct}%)"
    ), name
    assert decision.take_profit_tier == expected_tier, name
    assert decision.take_profit_pct == expected_pct, name


def test_stop_loss_unchanged_with_live_config():
    decision = evaluate_autosnipe_sell(_live_config_trade(), 0.69, {})
    assert decision.amount == 1000.0
    assert decision.message == "Auto-Sell All: Drops below 30%"
    assert decision.take_profit_tier is None


@pytest.mark.parametrize("price", [0.71, 0.93, 0.99])
def test_small_loss_above_stop_loss_does_not_sell(price):
    decision = evaluate_autosnipe_sell(_live_config_trade(), price, {})
    assert decision.amount == 0


def test_tier_already_sold_is_not_sold_again():
    trade = _live_config_trade(last_tp_tier=2.0, tp_pct_sold=10, initial_token_amount=1000.0,
                               purchased_token_amount=900.0)
    decision = evaluate_autosnipe_sell(trade, 2.4, {})
    assert decision.amount == 0


def test_next_tier_sells_its_share_of_original_position():
    trade = _live_config_trade(last_tp_tier=2.0, tp_pct_sold=10, initial_token_amount=1000.0,
                               purchased_token_amount=900.0)
    decision = evaluate_autosnipe_sell(trade, 3.2, {})
    assert decision.amount == pytest.approx(100.0)  # 10% of the original 1000, not of 900
    assert decision.take_profit_tier == 3.0
    assert decision.take_profit_pct == 20


def test_lower_tier_not_resold_after_price_falls_back():
    trade = _live_config_trade(last_tp_tier=5.0, tp_pct_sold=30, initial_token_amount=1000.0,
                               purchased_token_amount=700.0)
    decision = evaluate_autosnipe_sell(trade, 2.5, {1: 2.6})
    assert decision.amount == 0


def test_kumo_live_state_sells_its_missed_100_and_200_percent_tiers():
    # kumo sold only the 400% tier (10% of 30188.0010) under the previous deploy.
    trade = _live_config_trade(last_tp_tier=5.0, tp_pct_sold=10, initial_token_amount=30188.0010,
                               purchased_token_amount=27169.2008838)
    decision = evaluate_autosnipe_sell(trade, 7.5, {})
    assert decision.amount == pytest.approx(30188.0010 * 0.20)
    assert decision.take_profit_pct == 30


def test_clients_example_tiers_add_up_until_everything_is_sold():
    """At 100% sell 10, at 200% 10 more, at 400% 20 more, ... until all sold."""
    trade = _live_config_trade(sell_at_100=10, sell_at_200=10, sell_at_400=20, sell_at_1000=25,
                               sell_at_1500=35, initial_token_amount=1000.0)
    expected = [(2.1, 100.0, 10), (3.1, 100.0, 20), (5.2, 200.0, 40), (11.5, 250.0, 65), (16.5, 350.0, 100)]
    for price, amount, cumulative in expected:
        decision = evaluate_autosnipe_sell(trade, price, {})
        assert decision.amount == pytest.approx(amount), price
        assert decision.take_profit_pct == cumulative, price
        trade.purchased_token_amount -= decision.amount
        trade.tp_pct_sold = decision.take_profit_pct
        trade.last_tp_tier = decision.take_profit_tier
    assert trade.purchased_token_amount == pytest.approx(0)


def test_take_profit_never_sells_more_than_remaining():
    trade = _live_config_trade(sell_at_100=60, sell_at_200=60, initial_token_amount=1000.0,
                               tp_pct_sold=60, last_tp_tier=2.0, purchased_token_amount=400.0)
    decision = evaluate_autosnipe_sell(trade, 3.1, {})
    assert decision.amount == pytest.approx(400.0)
    assert decision.take_profit_pct == 100


def test_zero_pct_tier_is_skipped():
    trade = _live_config_trade(sell_at_200=0)
    decision = evaluate_autosnipe_sell(trade, 5.5, {})
    assert decision.amount == pytest.approx(200.0)  # 100% tier + 400% tier
    assert decision.take_profit_tier == 5.0
    assert decision.take_profit_pct == 20


def test_missing_initial_amount_falls_back_to_remaining():
    trade = _live_config_trade(initial_token_amount=None, purchased_token_amount=500.0)
    decision = evaluate_autosnipe_sell(trade, 2.0, {})
    assert decision.amount == pytest.approx(50.0)


def test_trailing_stop_after_100_percent_reachable_with_live_config():
    # Peak 5.0x, now 2.4x: 52% off the peak >= drop_after_100 (50%).
    decision = evaluate_autosnipe_sell(_live_config_trade(), 2.4, {1: 5.0})
    assert decision.amount == 1000.0
    assert "after 100% profit" in decision.message
    assert decision.take_profit_tier is None


# --- Wallet balance read --------------------------------------------------

TOKEN_2022 = sell_script.Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
OWNER = sell_script.Pubkey.from_string("8nVc5BL7xoq9QWQ4AoM2AAcca9DPPzgnqZuv9VqBP61F")
MINT_330 = "2zJ5SWBbsZLifcqWKm1uk6vcqWfWsLm3ud7NkafZpump"


def _expected_ata(owner, program, mint):
    return sell_script.Pubkey.find_program_address(
        [bytes(owner), bytes(program), bytes(sell_script.Pubkey.from_string(mint))],
        sell_script.ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def test_wallet_balance_reads_token_2022_ata_without_secondary_index():
    ata = _expected_ata(OWNER, TOKEN_2022, MINT_330)
    client = MagicMock()
    client.get_account_info.side_effect = lambda key: SimpleNamespace(
        value=SimpleNamespace(owner=TOKEN_2022)
    )
    client.get_token_account_balance.return_value = SimpleNamespace(
        value=SimpleNamespace(amount="17231344591", decimals=6)
    )

    with patch.object(sell_script, "solana_client", client):
        assert sell_script.get_wallet_token_balance(OWNER, MINT_330) == (17231344591, 6)

    client.get_token_account_balance.assert_called_once_with(ata)
    client.get_token_accounts_by_owner_json_parsed.assert_not_called()


def test_wallet_balance_missing_ata_is_zero():
    client = MagicMock()
    client.get_account_info.side_effect = [
        SimpleNamespace(value=SimpleNamespace(owner=TOKEN_2022)),
        SimpleNamespace(value=None),
    ]
    with patch.object(sell_script, "solana_client", client):
        assert sell_script.get_wallet_token_balance(OWNER, MINT_330) == (0, None)


def test_wallet_balance_missing_mint_raises():
    client = MagicMock()
    client.get_account_info.return_value = SimpleNamespace(value=None)
    with patch.object(sell_script, "solana_client", client):
        with pytest.raises(ValueError):
            sell_script.get_wallet_token_balance(OWNER, MINT_330)


# --- Sell loop (scheduler job) --------------------------------------------

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
def harness(tmp_path):
    """Captures the scheduler job and stubs every external dependency."""
    keypair = Keypair()
    key_file = tmp_path / "wallet.key"
    key_file.write_bytes(bytes(keypair))
    _FakeVersionedTx.wallet_pubkey = keypair.pubkey()

    h = SimpleNamespace(trades=[], prices={}, jobs=[], quote_status={}, send_error=None, balances={},
                        wallets={}, price_delays={}, events=[])

    class _Scheduler:
        def __init__(self, *a, **k):
            pass

        def add_job(self, func, *a, **k):
            h.jobs.append(func)

        def start(self):
            pass

    trade_model = MagicMock()
    trade_model.query.filter_by.return_value.order_by.return_value.all.side_effect = (
        lambda: [t for t in h.trades if not t.executed]
    )
    default_wallet = SimpleNamespace(public_key="WalletPubkey", private_key=str(key_file))
    wallet_model = MagicMock()
    wallet_model.query.filter_by.side_effect = lambda **kw: SimpleNamespace(
        first=lambda: h.wallets.get(kw.get("public_key"), default_wallet)
    )

    def _quote(url, params=None, **kwargs):
        h.events.append(("quote", params["inputMint"], time.monotonic()))
        status = h.quote_status.get(params["inputMint"], 200)
        if status != 200:
            return _response(status, {"error": "No routes found", "errorCode": "NO_ROUTES_FOUND"})
        return _response(200, {"inAmount": str(params["amount"])})

    requests_mock = MagicMock()
    requests_mock.get.side_effect = _quote
    requests_mock.post.return_value = _response(
        200, {"swapTransaction": base64.b64encode(b"tx").decode()}
    )

    def _send(_tx):
        if h.send_error:
            raise RuntimeError(h.send_error)
        h.sent += 1
        return SimpleNamespace(value=f"sig{h.sent}")

    h.sent = 0
    solana_mock = MagicMock()
    solana_mock.send_transaction.side_effect = _send

    def _price(mint):
        time.sleep(h.price_delays.get(mint, 0))
        h.events.append(("priced", mint, time.monotonic()))
        price = h.prices[mint]
        if isinstance(price, Exception):
            raise price
        return {"usdPrice": price}

    def _balance(owner, mint):
        balance = h.balances.get(mint, 10**12)
        if isinstance(balance, Exception):
            raise balance
        return balance, 6

    h.history = MagicMock()
    h.logger = MagicMock()
    h.requests = requests_mock
    sell_script.price_tracking.clear()

    with patch.object(sell_script, "BackgroundScheduler", _Scheduler), \
         patch.object(sell_script, "atexit", MagicMock()), \
         patch.object(sell_script, "Trade", trade_model), \
         patch.object(sell_script, "Wallet", wallet_model), \
         patch.object(sell_script, "TradeHistory", h.history), \
         patch.object(sell_script, "db", MagicMock()), \
         patch.object(sell_script, "requests", requests_mock), \
         patch.object(sell_script, "solana_client", solana_mock), \
         patch.object(sell_script, "VersionedTransaction", _FakeVersionedTx), \
         patch.object(sell_script, "get_wallet_token_balance", side_effect=_balance), \
         patch.object(sell_script, "get_sniper_sell_price", side_effect=_price), \
         patch.object(sell_script, "logger", h.logger):
        sell_script.auto_snipe_auto_sell_schedular(Flask("test"))
        h.run_cycle = h.jobs[0]
        yield h


def _logged(h, level):
    return " ".join(str(c.args[0]) for c in getattr(h.logger, level).call_args_list)


def _quoted_amounts(h):
    return [c.kwargs["params"]["amount"] for c in h.requests.get.call_args_list]


def test_loop_full_exit_sells_actual_balance_when_less_than_stored(harness):
    """Live case: stored amount is the buy quote estimate; wallet holds ~4% less."""
    sl = _live_config_trade(id=3, token_address="MintSL", purchased_token_amount=18038.94341)
    harness.trades = [sl]
    harness.prices = {"MintSL": 0.5}
    harness.balances = {"MintSL": 17231344591}  # trade 330's real on-chain raw balance

    harness.run_cycle()

    assert _quoted_amounts(harness) == [17231344591]
    assert sl.executed is True
    assert sl.purchased_token_amount == 0
    assert harness.history.call_args.kwargs["amount"] == pytest.approx(17231.344591)


def test_loop_partial_take_profit_uses_requested_amount_when_covered(harness):
    tp = _live_config_trade(id=2, token_address="MintTP", purchased_token_amount=1000.0)
    harness.trades = [tp]
    harness.prices = {"MintTP": 2.33}
    harness.balances = {"MintTP": 950_000_000}

    harness.run_cycle()

    assert _quoted_amounts(harness) == [100_000_000]
    assert tp.purchased_token_amount == pytest.approx(900.0)
    assert tp.last_tp_tier == 2.0


def test_loop_zero_wallet_balance_skips_without_quoting(harness):
    sl = _live_config_trade(id=3, token_address="MintGone")
    harness.trades = [sl]
    harness.prices = {"MintGone": 0.5}
    harness.balances = {"MintGone": 0}

    harness.run_cycle()

    assert _quoted_amounts(harness) == []
    assert sl.executed is False
    assert "Wallet holds 0 tokens for trade 3 (MintGone)" in _logged(harness, "warning")


def test_loop_balance_read_failure_skips_trade(harness):
    sl = _live_config_trade(id=3, token_address="MintSL")
    harness.trades = [sl]
    harness.prices = {"MintSL": 0.5}
    harness.balances = {"MintSL": RuntimeError("RPC timeout")}

    harness.run_cycle()

    assert _quoted_amounts(harness) == []
    assert sl.executed is False
    assert "Could not read wallet token balance for trade 3 (MintSL)" in _logged(harness, "warning")


def test_loop_take_profit_and_stop_loss_in_same_cycle(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    sl = _live_config_trade(id=3, token_address="MintSL")
    harness.trades = [tp, sl]
    harness.prices = {"MintTP": 2.33, "MintSL": 0.5}

    harness.run_cycle()

    assert harness.sent == 2
    assert tp.purchased_token_amount == pytest.approx(900.0)
    assert tp.last_tp_tier == 2.0
    assert tp.tp_pct_sold == 10
    assert tp.initial_token_amount == 1000.0  # captured before the first take-profit sale
    assert tp.executed is False
    assert sl.purchased_token_amount == 0
    assert sl.executed is True


def test_loop_does_not_resell_same_tier_next_cycle(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    harness.trades = [tp]
    harness.prices = {"MintTP": 2.33}

    harness.run_cycle()
    harness.run_cycle()
    harness.run_cycle()

    assert harness.sent == 1
    assert tp.purchased_token_amount == pytest.approx(900.0)


def test_loop_price_climbing_through_tiers_sells_each_once(harness):
    tp = _live_config_trade(id=2, token_address="MintTP", initial_token_amount=1000.0)
    harness.trades = [tp]
    for price in (2.1, 2.2, 3.5, 3.6, 7.5, 7.0):
        harness.prices = {"MintTP": price}
        harness.run_cycle()

    assert _quoted_amounts(harness) == [100_000_000, 100_000_000, 100_000_000]
    assert tp.purchased_token_amount == pytest.approx(700.0)
    assert tp.tp_pct_sold == 30
    assert tp.last_tp_tier == 5.0


def test_loop_jupiter_quote_failure_keeps_trade_retryable(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    other = _live_config_trade(id=3, token_address="MintOther")
    harness.trades = [tp, other]
    harness.prices = {"MintTP": 2.33, "MintOther": 0.5}
    harness.quote_status = {"MintTP": 400}

    harness.run_cycle()

    assert tp.purchased_token_amount == 1000.0
    assert tp.last_tp_tier == 0
    assert "trade 2 (MintTP)" in _logged(harness, "error")
    assert other.executed is True  # failure didn't stop the rest of the cycle

    harness.quote_status = {}
    harness.run_cycle()
    assert tp.last_tp_tier == 2.0
    assert tp.purchased_token_amount == pytest.approx(900.0)


def test_loop_failed_send_does_not_record_sale(harness):
    tp = _live_config_trade(id=2, token_address="MintTP")
    harness.trades = [tp]
    harness.prices = {"MintTP": 2.33}
    harness.send_error = "Blockhash not found"

    harness.run_cycle()

    assert tp.purchased_token_amount == 1000.0
    assert tp.last_tp_tier == 0
    harness.history.assert_not_called()
    assert "trade 2 (MintTP)" in _logged(harness, "error")


def test_loop_price_fetch_failure_skips_only_that_trade(harness):
    bad = _live_config_trade(id=2, token_address="MintBad")
    tp = _live_config_trade(id=3, token_address="MintTP")
    harness.trades = [bad, tp]
    harness.prices = {"MintBad": RuntimeError("Shyft 429"), "MintTP": 2.0}

    harness.run_cycle()

    assert bad.purchased_token_amount == 1000.0
    assert tp.last_tp_tier == 2.0


def test_loop_missing_key_file_does_not_abort_cycle(harness, tmp_path):
    """Regression: a missing key file used to `return` out of the whole cycle."""
    first = _live_config_trade(id=5, token_address="MintMissingKey", to_pubkey="GoneWallet")
    second = _live_config_trade(id=4, token_address="MintTP")
    harness.trades = [first, second]
    harness.prices = {"MintMissingKey": 2.33, "MintTP": 2.33}
    harness.price_delays = {"MintTP": 0.2}  # processed after the missing-key trade
    harness.wallets["GoneWallet"] = SimpleNamespace(
        public_key="GoneWallet", private_key=str(tmp_path / "missing.key")
    )

    harness.run_cycle()

    assert first.purchased_token_amount == 1000.0
    assert second.last_tp_tier == 2.0
    assert "Private key file missing" in _logged(harness, "error")


def test_loop_prices_are_fetched_in_parallel(harness):
    harness.trades = [_live_config_trade(id=i, token_address=f"Mint{i}") for i in range(8)]
    harness.prices = {f"Mint{i}": 1.2 for i in range(8)}  # no sell, just pricing
    harness.price_delays = {f"Mint{i}": 0.5 for i in range(8)}

    with patch.object(sell_script, "SNIPER_SELL_PRICE_WORKERS", 10):
        start = time.monotonic()
        harness.run_cycle()
        elapsed = time.monotonic() - start

    assert len([e for e in harness.events if e[0] == "priced"]) == 8
    assert elapsed < 1.5  # sequential would take 8 x 0.5s = 4s


def test_loop_fast_priced_trade_sells_before_slow_price_arrives(harness):
    slow = _live_config_trade(id=2, token_address="MintSlowDead")
    fast = _live_config_trade(id=3, token_address="MintFastSL")
    harness.trades = [slow, fast]  # the slow one comes first in DB order
    harness.prices = {"MintSlowDead": 1.1, "MintFastSL": 0.5}
    harness.price_delays = {"MintSlowDead": 1.0}

    harness.run_cycle()

    fast_quote = next(t for kind, mint, t in harness.events if kind == "quote" and mint == "MintFastSL")
    slow_priced = next(t for kind, mint, t in harness.events if kind == "priced" and mint == "MintSlowDead")
    assert fast_quote < slow_priced
    assert fast.executed is True


def test_loop_single_worker_still_processes_every_trade(harness):
    trades = [_live_config_trade(id=i, token_address=f"MintSL{i}") for i in range(5)]
    harness.trades = trades
    harness.prices = {f"MintSL{i}": 0.5 for i in range(5)}

    with patch.object(sell_script, "SNIPER_SELL_PRICE_WORKERS", 1):
        harness.run_cycle()

    assert harness.sent == 5
    assert all(t.executed for t in trades)


def test_loop_many_trades_each_processed_exactly_once(harness):
    trades = [_live_config_trade(id=i, token_address=f"Mint{i}") for i in range(40)]
    harness.trades = trades
    harness.prices = {f"Mint{i}": (0.5 if i % 2 else 2.2) for i in range(40)}  # SL / 100% TP
    harness.price_delays = {f"Mint{i}": 0.01 * (i % 7) for i in range(40)}

    harness.run_cycle()

    assert harness.sent == 40
    quoted = [mint for kind, mint, _ in harness.events if kind == "quote"]
    assert sorted(quoted) == sorted(f"Mint{i}" for i in range(40))
    for i, t in enumerate(trades):
        if i % 2:
            assert t.executed is True
        else:
            assert t.tp_pct_sold == 10 and t.purchased_token_amount == pytest.approx(900.0)
