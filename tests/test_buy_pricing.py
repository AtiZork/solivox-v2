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

    h.trade_model = MagicMock()
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
    """Regression guard: no Shyft-sourced price may reach the Trade record
    (metadata-only Shyft calls - get_token_details - are fine)."""
    with patch.object(buy, "get_token_price") as shyft_price_fetch:
        buy.buy_token(MINT, _config())
    shyft_price_fetch.assert_not_called()


def test_token_details_lookup_failure_falls_back_safely(harness):
    harness.shyft_service.get_token_details.side_effect = RuntimeError("shyft rest down")
    assert buy.buy_token(MINT, _config()) is True
    kwargs = _trade_kwargs(harness)
    assert kwargs["token_name"] == "Unknown"
    assert kwargs["token_symbol"] == "UNKNOWN"
    assert len(harness.sent) == 1  # buy still completed


def test_token_details_lookup_success_is_used(harness):
    buy.buy_token(MINT, _config())
    kwargs = _trade_kwargs(harness)
    assert kwargs["token_name"] == "DemoCoin"
    assert kwargs["token_symbol"] == "DEMO"


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
