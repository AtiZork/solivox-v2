"""
Buy-time pricing: no Geyser, no Shyft, no live price of any kind on the
buy's hot path. initial_price is recorded as a 0.0 placeholder immediately
(autosnipe_sell_logic treats initial_price<=0 as "nothing to evaluate yet",
so this is always safe); reconcile_pending_buys() (tested separately in
test_buy_reconciliation.py) fills in the real price afterward from the
confirmed transaction's actual execution data.
"""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from solders.keypair import Keypair

import autosnipe_buy_new_token as buy

MINT = "BbwMpDMkXf4VE8p2gBFPnv1sP8kwHmmHyP7ma6yfpump"


def _config(user_id=1, buy_amount=0.01):
    fields = dict(
        user_id=user_id, active=True, buy_amount=buy_amount, slippage=30, priority_fee=0.0001,
        drop_cutoff=30, drop_until_profit=99, drop_after_100=50, drop_after_400=30,
    )
    for tier in (100, 200, 400, 1000, 1500, 2500, 4000, 10000):
        fields[f"sell_at_{tier}"] = 10
    return SimpleNamespace(**fields)


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
    seed = os.urandom(32)
    key_file = tmp_path / "wallet.key"
    key_file.write_bytes(seed)
    _FakeVersionedTx.wallet_pubkey = Keypair.from_seed(seed).pubkey()

    h = SimpleNamespace(sent=[])

    def _send(tx, opts=None):
        h.sent.append(opts)
        return SimpleNamespace(value=f"sig{len(h.sent)}")

    client = MagicMock()
    client.send_transaction.side_effect = _send
    client.get_balance.return_value = SimpleNamespace(value=10 * 10**9)

    requests_mock = MagicMock()
    requests_mock.get.return_value = _response(200, {"outAmount": "1000000000", "outputMintDecimals": 6})
    requests_mock.post.return_value = _response(200, {"swapTransaction": base64.b64encode(b"tx").decode()})

    wallet = SimpleNamespace(public_key=str(_FakeVersionedTx.wallet_pubkey), private_key=str(key_file))
    wallet_model = MagicMock()
    wallet_model.query.filter_by.return_value.first.return_value = wallet
    # Also covers the deterministic fallback path (.order_by(Wallet.id.asc()).first()).
    wallet_model.query.filter_by.return_value.order_by.return_value.first.return_value = wallet

    h.trade_model = MagicMock()
    # No existing AUTOSNIPE trade for this (user, mint) by default (the B10
    # dedup check) - individual tests override this to simulate a duplicate.
    h.trade_model.query.filter_by.return_value.first.return_value = None
    h.history_model = MagicMock()
    h.logger = MagicMock()
    h.shyft_service = MagicMock(get_token_details=MagicMock(return_value={"name": "DemoCoin", "symbol": "DEMO"}))
    buy.BOUGHT_TOKENS.clear()

    with patch.object(buy, "solana_client", client), \
         patch.object(buy, "requests", requests_mock), \
         patch.object(buy, "Wallet", wallet_model), \
         patch.object(buy, "Trade", h.trade_model), \
         patch.object(buy, "TradeHistory", h.history_model), \
         patch.object(buy, "db", MagicMock()), \
         patch.object(buy, "VersionedTransaction", _FakeVersionedTx), \
         patch.object(buy, "get_shyft_pricing_service", return_value=h.shyft_service), \
         patch.object(buy, "logger", h.logger), \
         patch("latency_log.logger", MagicMock()):
        yield h
    buy.BOUGHT_TOKENS.clear()


def _trade_kwargs(h):
    return h.trade_model.call_args.kwargs


def _history_kwargs(h):
    return h.history_model.call_args.kwargs


def test_initial_price_is_zero_placeholder(harness):
    assert buy.buy_token(MINT, _config(buy_amount=0.5)) is True
    kwargs = _trade_kwargs(harness)
    assert kwargs["initial_price"] == 0.0
    assert kwargs["amount"] == pytest.approx(0.5)


# --- Buy dedup survives a restart (B10) ------------------------------------

def test_duplicate_buy_blocked_by_db_even_with_empty_in_memory_set(harness):
    """Regression: BOUGHT_TOKENS is in-memory only and cleared by a restart.
    Simulates that: the in-memory set is empty (as it would be right after a
    restart), but an AUTOSNIPE Trade for this exact (user, mint) already
    exists in the DB - the buy must still be blocked."""
    buy.BOUGHT_TOKENS.clear()
    harness.trade_model.query.filter_by.return_value.first.return_value = SimpleNamespace(id=99)

    assert buy.buy_token(MINT, _config(buy_amount=0.5)) is False
    assert harness.sent == []  # never reached send_transaction


def test_first_buy_for_a_mint_is_not_blocked(harness):
    assert buy.buy_token(MINT, _config(buy_amount=0.5)) is True
    assert len(harness.sent) == 1


def test_sell_priority_fee_is_taken_from_config(harness):
    """Regression (C4): previously nothing ever set Trade.default_gas_fee
    for an AUTOSNIPE trade, so every snipe sell's priority fee was stuck at
    that column's own hardcoded default (0.01) no matter what the sniper
    config asked for."""
    config = _config(buy_amount=0.5)
    config.sell_priority_fee = 0.025
    assert buy.buy_token(MINT, config) is True
    assert _trade_kwargs(harness)["default_gas_fee"] == pytest.approx(0.025)


def test_sell_priority_fee_defaults_when_config_omits_it(harness):
    """A config saved before sell_priority_fee existed (missing the
    attribute entirely) must still produce a sane default, not crash."""
    config = _config(buy_amount=0.5)  # no sell_priority_fee set
    assert buy.buy_token(MINT, config) is True
    assert _trade_kwargs(harness)["default_gas_fee"] == pytest.approx(0.01)


def test_purchased_token_amount_uses_quote_as_provisional_estimate(harness):
    """outAmount=1_000_000_000 raw / 10**6 decimals = 1000.0 tokens - the
    provisional figure, corrected later by reconciliation."""
    buy.buy_token(MINT, _config())
    kwargs = _trade_kwargs(harness)
    assert kwargs["purchased_token_amount"] == pytest.approx(1000.0)
    assert kwargs["initial_token_amount"] == pytest.approx(1000.0)


def test_trade_history_execution_price_matches_trade_initial_price(harness):
    buy.buy_token(MINT, _config(buy_amount=1.0))
    assert _history_kwargs(harness)["execution_price"] == _trade_kwargs(harness)["initial_price"]


def test_trade_history_status_is_sent_pending_reconciliation(harness):
    buy.buy_token(MINT, _config())
    assert _history_kwargs(harness)["status"] == "sent"


def test_no_geyser_module_imported_or_called(harness):
    """Regression guard for the actual requirement: the buy flow must not
    touch Yellowstone/Geyser at all for entry pricing."""
    assert not hasattr(buy, "get_yellowstone_price")
    assert not hasattr(buy, "is_yellowstone_enabled")


def test_shyft_get_token_price_never_called_for_pricing(harness):
    """Regression guard: no Shyft-sourced price may reach the Trade record.
    Shyft is not used for anything at all on the buy's hot path - not
    pricing, not token metadata (see the token_name/token_symbol tests
    below)."""
    with patch.object(buy, "get_token_price") as shyft_price_fetch:
        buy.buy_token(MINT, _config())
    shyft_price_fetch.assert_not_called()


def test_token_name_and_symbol_are_static_not_fetched_from_shyft(harness):
    """Regression: token_name/token_symbol used to be fetched from Shyft
    (get_shyft_pricing_service().get_token_details()) on the buy's hot path.
    Per explicit instruction, Shyft must not be used here at all, even for
    metadata - these are now always the static "Unknown"/"UNKNOWN" values,
    and get_shyft_pricing_service is never called."""
    assert buy.buy_token(MINT, _config()) is True
    kwargs = _trade_kwargs(harness)
    assert kwargs["token_name"] == "Unknown"
    assert kwargs["token_symbol"] == "UNKNOWN"
    harness.shyft_service.get_token_details.assert_not_called()


def test_buy_completes_with_zero_quoted_tokens_without_crashing(harness):
    with patch.object(buy.requests, "get", return_value=_response(200, {"outAmount": "0", "outputMintDecimals": 6})):
        assert buy.buy_token(MINT, _config()) is True
    assert _trade_kwargs(harness)["purchased_token_amount"] == 0.0


def test_buy_has_no_geyser_latency_stage(harness):
    """The buy's hot path must not contain any Geyser-related timing stage
    - confirms no live price fetch of any kind happens during the buy."""
    h_latency = MagicMock()
    with patch("latency_log.logger", h_latency):
        buy.buy_token(MINT, _config(), decision_ms=1.0)
    line = h_latency.info.call_args_list[0].args[0]
    assert "geyser" not in line.lower()


# --- get_sol_usd_rate() -------------------------------------------------
#
# Direct, first-party SOL/USD read for the entry-price calculation: a plain
# point-in-time getAccountInfo on Pyth's SOL/USD account via solana_client
# (our own RPC node) - not Geyser (no subscribe/wait, so none of its
# "nothing traded, nothing pushed" unreliability applies) and not Shyft (no
# dependency on Shyft's RPC endpoint).

PYTH_RAW_LEN = 134


def _pyth_raw(price: int, expo: int) -> bytes:
    raw = bytearray(PYTH_RAW_LEN)
    raw[73:81] = int(price).to_bytes(8, "little", signed=True)
    raw[89:93] = int(expo).to_bytes(4, "little", signed=True)
    return bytes(raw)


def test_get_sol_usd_rate_parses_real_pyth_layout():
    # price=10132933276, expo=-8 -> 101.32933276 (matches a real on-chain read)
    raw = _pyth_raw(10132933276, -8)
    account = SimpleNamespace(data=raw)
    resp = SimpleNamespace(value=account)
    with patch.object(buy.solana_client, "get_account_info", return_value=resp):
        assert buy.get_sol_usd_rate() == pytest.approx(101.32933276)


def test_get_sol_usd_rate_returns_none_when_account_missing():
    resp = SimpleNamespace(value=None)
    with patch.object(buy.solana_client, "get_account_info", return_value=resp):
        assert buy.get_sol_usd_rate() is None


def test_get_sol_usd_rate_returns_none_on_rpc_failure():
    with patch.object(buy, "logger", MagicMock()), \
         patch.object(buy.solana_client, "get_account_info", side_effect=RuntimeError("rpc down")):
        assert buy.get_sol_usd_rate() is None  # never raises


def test_get_sol_usd_rate_returns_none_on_truncated_data():
    account = SimpleNamespace(data=b"\x00" * 10)
    resp = SimpleNamespace(value=account)
    with patch.object(buy.solana_client, "get_account_info", return_value=resp):
        assert buy.get_sol_usd_rate() is None


def test_get_sol_usd_rate_does_not_use_shyft_rpc():
    """Regression guard: this must go through solana_client (our own node),
    never through get_shyft_pricing_service()."""
    with patch.object(buy, "get_shyft_pricing_service") as shyft_svc:
        raw = _pyth_raw(10000000000, -8)
        resp = SimpleNamespace(value=SimpleNamespace(data=raw))
        with patch.object(buy.solana_client, "get_account_info", return_value=resp):
            buy.get_sol_usd_rate()
    shyft_svc.assert_not_called()


# --- Wallet selection (B12) -------------------------------------------------

def test_buy_prefers_configured_wallet_id_over_the_fallback_query(harness):
    """Regression: a user with more than one wallet must have the snipe buy
    look up the wallet their AutoSnipeConfig.wallet_id actually points at
    (filter_by(id=...)), not go straight to the "first wallet" fallback
    query (filter_by(user_id=...).order_by(...)) that used to be the only
    lookup - that one isn't guaranteed to pick the same wallet every time
    for a user with more than one."""
    fallback_wallet = SimpleNamespace(public_key="FallbackWalletShouldNotBeUsed11111111111111", private_key="x")
    configured_wallet = buy.Wallet.query.filter_by.return_value.first.return_value  # the harness's real wallet

    filter_by_calls = []

    def _filter_by(**kw):
        filter_by_calls.append(kw)
        result = MagicMock()
        if "id" in kw:
            result.first.return_value = configured_wallet
        else:
            result.order_by.return_value.first.return_value = fallback_wallet
        return result

    buy.Wallet.query.filter_by.side_effect = _filter_by

    config = _config(buy_amount=0.5)
    config.wallet_id = 7

    assert buy.buy_token(MINT, config) is True

    assert {"id": 7, "user_id": config.user_id} in filter_by_calls
    kwargs = _trade_kwargs(harness)
    assert kwargs["to_pubkey"] == configured_wallet.public_key  # not the fallback wallet


def test_buy_falls_back_to_first_wallet_when_wallet_id_unset(harness):
    """A config saved before wallet_id existed (or left unset) must still
    work exactly as before: falls back to the deterministic first-wallet
    query, not an error."""
    config = _config(buy_amount=0.5)  # no wallet_id set at all
    assert buy.buy_token(MINT, config) is True
    assert "id" not in buy.Wallet.query.filter_by.call_args.kwargs
