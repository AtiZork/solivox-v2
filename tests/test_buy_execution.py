"""
Buy execution: preflight commitment, send-error reporting and duplicate-buy
protection under concurrency. Nothing here touches the network.

Live check before the fix (fresh Pump.fun launches, 2s old, simulation only):
a Jupiter buy simulated at finalized failed for 4 of 4 tokens (Jupiter
0x1783) and passed at confirmed for the same 4 — finalized was 31 slots behind
and the token didn't exist there yet. solana-py's Client defaults to
finalized, so send_transaction() preflighted against that stale bank.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from solana.rpc.commitment import Confirmed
from solana.rpc.core import RPCException
from solders.keypair import Keypair
from solders.rpc.responses import SendTransactionResp

import autosnipe_buy_new_token as buy

MINT = "BbwMpDMkXf4VE8p2gBFPnv1sP8kwHmmHyP7ma6yfpump"


def _preflight_error(err_json: str) -> RPCException:
    """The exact error object solana-py raises for a rejected preflight."""
    body = (
        '{"jsonrpc":"2.0","error":{"code":-32002,"message":"Transaction simulation failed: '
        'Error processing Instruction 5: incorrect program id for instruction","data":{"accounts":null,'
        f'"err":{err_json},"innerInstructions":null,"logs":["Program JUP6 invoke [1]",'
        '"Program JUP6 failed: incorrect program id for instruction"],"replacementBlockhash":null,'
        '"returnData":null,"unitsConsumed":0}},"id":1}'
    )
    return RPCException(SendTransactionResp.from_json(body))


def _config(user_id=1):
    fields = dict(
        user_id=user_id, active=True, buy_amount=0.01, slippage=30, priority_fee=0.0001,
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

    h = SimpleNamespace(sent=[], send_error=None, send_delay=0.0, active=0, max_active=0)
    guard = threading.Lock()

    def _send(tx, opts=None):
        with guard:
            h.active += 1
            h.max_active = max(h.max_active, h.active)
        try:
            time.sleep(h.send_delay)
            if h.send_error:
                raise h.send_error
            h.sent.append(opts)
            return SimpleNamespace(value=f"sig{len(h.sent)}")
        finally:
            with guard:
                h.active -= 1

    client = MagicMock()
    client.send_transaction.side_effect = _send
    client.get_balance.return_value = SimpleNamespace(value=10 * 10**9)
    h.client = client

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
    h.trade_history_model = MagicMock()
    h.logger = MagicMock()
    h.latency = MagicMock()
    h.db = MagicMock()
    h.requests = requests_mock
    buy.BOUGHT_TOKENS.clear()

    with patch.object(buy, "solana_client", client), \
         patch.object(buy, "requests", requests_mock), \
         patch.object(buy, "Wallet", wallet_model), \
         patch.object(buy, "Trade", h.trade_model), \
         patch.object(buy, "TradeHistory", h.trade_history_model), \
         patch.object(buy, "db", h.db), \
         patch.object(buy, "VersionedTransaction", _FakeVersionedTx), \
         patch.object(buy, "get_token_price", return_value={"usdPrice": 1e-6, "name": "N", "symbol": "S"}), \
         patch.object(buy, "get_shyft_pricing_service", return_value=MagicMock(
             get_token_details=MagicMock(return_value={"name": "N", "symbol": "S"})
         )), \
         patch.object(buy, "logger", h.logger), \
         patch("latency_log.logger", h.latency):
        yield h
    buy.BOUGHT_TOKENS.clear()


def _latency_lines(h):
    return [c.args[0] for c in h.latency.info.call_args_list]


def test_send_preflights_at_confirmed(harness):
    assert buy.buy_token(MINT, _config()) is True
    assert len(harness.sent) == 1
    assert harness.sent[0].preflight_commitment == Confirmed
    assert harness.sent[0].skip_preflight is False  # still simulated, just on a current bank
    assert harness.sent[0].skip_confirmation is True  # no added wait for confirmation


def test_sol_balance_read_at_confirmed(harness):
    buy.buy_token(MINT, _config())
    assert harness.client.get_balance.call_args.kwargs["commitment"] == Confirmed


@pytest.mark.parametrize("err_json, expected", [
    ('{"InstructionError":[5,"IncorrectProgramId"]}', "IncorrectProgramId"),
    ('"BlockhashNotFound"', "BlockhashNotFound"),
    ('{"InstructionError":[5,{"Custom":6019}]}', "6019"),
])
def test_preflight_rejection_is_reported_not_crashed(harness, err_json, expected):
    """The old handler read error.data.err.err.code, which raised AttributeError
    for IncorrectProgramId and BlockhashNotFound (only Custom errors have
    .code), hiding the real reason behind a generic "An error occurred"."""
    harness.send_error = _preflight_error(err_json)

    assert buy.buy_token(MINT, _config()) is True

    logged = " ".join(str(c.args[0]) for c in harness.logger.error.call_args_list)
    assert f"Buy transaction for {MINT} rejected" in logged
    assert expected in logged
    assert "incorrect program id" in logged  # the failing program log line
    # The Trade row is written before send (action-plan 1.4a) and is kept
    # even though this send raised: the error doesn't prove the transaction
    # never landed, so deleting the row here could erase the only record of
    # a buy that actually went through. Only the TradeHistory row (which
    # needs a real signature) is skipped for this outcome.
    harness.trade_model.assert_called_once()
    harness.trade_history_model.assert_not_called()
    assert "outcome=send_rejected" in _latency_lines(harness)[0]


def test_trade_row_is_written_before_the_swap_is_even_built(harness):
    """Action-plan 1.4a: the Trade row used to be created only after
    send_transaction succeeded, so a real on-chain buy with a DB write that
    failed afterward left tokens in the wallet with no Trade row at all. It's
    now written right after the quote (before the swap transaction is even
    fetched), using the fields that step already has - never after send."""
    assert buy.buy_token(MINT, _config()) is True
    harness.trade_model.assert_called_once()
    kwargs = harness.trade_model.call_args.kwargs
    assert kwargs["purchased_token_amount"] == 1000.0  # from the quote's outAmount
    assert kwargs["initial_token_amount"] == 1000.0


def test_swap_setup_failure_deletes_the_just_written_pending_trade(harness):
    """Unlike a rejected send (which may have landed despite the error - see
    test_preflight_rejection_is_reported_not_crashed), a failure while
    building/fetching the swap transaction happens strictly before anything
    is ever sent on-chain. Nothing could have landed, so the pending Trade
    row written right after the quote must be cleaned up rather than left
    behind as a phantom position."""
    harness.requests.post.return_value = _response(500, {"error": "swap service down"})

    assert buy.buy_token(MINT, _config()) is True

    harness.trade_model.assert_called_once()  # written right after the quote
    created_trade = harness.trade_model.return_value
    harness.db.session.delete.assert_called_once_with(created_trade)
    harness.trade_history_model.assert_not_called()  # no signature was ever obtained
    assert harness.sent == []  # send_transaction was never reached


def test_open_autosnipe_trade_for_mint_skips_the_buy(harness):
    """B10/action-plan 1.3: an existing OPEN AUTOSNIPE trade for this (user,
    mint) blocks a rebuy - the dedup query must filter on executed=False,
    matching the DB-level partial unique index's own scope."""
    harness.trade_model.query.filter_by.return_value.first.return_value = SimpleNamespace(id=99)

    assert buy.buy_token(MINT, _config()) is False

    harness.trade_model.query.filter_by.assert_any_call(
        user_id=1, token_address=MINT, auto_snipe=True, executed=False
    )
    assert harness.sent == []


def test_closed_autosnipe_trade_for_mint_allows_a_rebuy(harness):
    """Action-plan 1.3 deliberately scoped the dedup to open positions, not
    forever: once a position is fully closed (executed=True), the mint is
    tradable again. The query above already filters executed=False, so a
    closed row simply never matches - this proves the buy still goes
    through when the mock's default (no match) represents that."""
    harness.trade_model.query.filter_by.return_value.first.return_value = None

    assert buy.buy_token(MINT, _config()) is True

    assert len(harness.sent) == 1


def test_integrity_error_on_pending_trade_commit_is_treated_as_duplicate(harness):
    """Defense in depth (action-plan 1.3) for a race the query check and
    WALLET_BUY_LOCK should already prevent: if the DB's own partial unique
    index ever rejects the insert anyway, it's reported as a duplicate, not
    a crash, and nothing downstream (swap/send) is ever reached."""
    from sqlalchemy.exc import IntegrityError
    harness.db.session.commit.side_effect = IntegrityError("duplicate key", {}, BaseException())

    assert buy.buy_token(MINT, _config()) is False

    assert harness.sent == []
    assert harness.requests.post.call_count == 0  # swap was never reached
    harness.db.session.rollback.assert_called_once()


def test_describe_send_error_handles_plain_exceptions():
    assert buy.describe_send_error(RuntimeError("connection reset")) == "connection reset"


def test_same_user_and_mint_bought_once_under_concurrency(harness):
    harness.send_delay = 0.05
    config = _config()
    threads = [threading.Thread(target=buy.buy_token, args=(MINT, config)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(harness.sent) == 1
    assert harness.trade_model.call_count == 1


def test_different_users_each_buy_the_mint_once(harness):
    for user in (1, 2, 1, 2):
        buy.buy_token(MINT, _config(user_id=user))
    assert len(harness.sent) == 2


def test_buys_still_serialized_by_wallet_lock(harness):
    harness.send_delay = 0.05
    threads = [threading.Thread(target=buy.buy_token, args=(f"Mint{i}pump", _config())) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(harness.sent) == 5
    assert harness.max_active == 1  # global lock kept: never two sends in flight


def test_ambiguous_send_failure_is_never_resent(harness):
    """A timeout may still have landed on chain: the mint must not be bought again."""
    harness.send_error = TimeoutError("read timed out")
    buy.buy_token(MINT, _config())
    harness.send_error = None
    buy.buy_token(MINT, _config())
    assert len(harness.sent) == 0
    assert harness.client.send_transaction.call_count == 1


def test_quote_failure_does_not_block_a_later_attempt(harness):
    """Nothing was sent, so the mint isn't marked as bought."""
    with patch.object(buy.requests, "get", return_value=_response(400, {"errorCode": "NO_ROUTES_FOUND"})):
        buy.buy_token(MINT, _config())
    assert (1, MINT) not in buy.BOUGHT_TOKENS
    buy.buy_token(MINT, _config())
    assert len(harness.sent) == 1


def test_latency_line_has_every_stage(harness):
    # No token_details stage: token name/symbol are static ("Unknown"), not
    # fetched (from Shyft or anywhere else), so there's no lookup to time.
    buy.buy_token(MINT, _config(), decision_ms=1234.5)
    line = _latency_lines(harness)[0]
    assert line.startswith("[latency] path=buy")
    assert f"mint={MINT}" in line and "outcome=sent" in line and "sig=sig1" in line
    for stage in ("decision=1234.5", "lock_wait=", "balance=", "quote=", "swap=", "send=", "db=", "total="):
        assert stage in line, stage


def test_lock_wait_is_measured(harness):
    with buy.WALLET_BUY_LOCK:
        t = threading.Thread(target=buy.buy_token, args=(MINT, _config()))
        t.start()
        time.sleep(0.15)
    t.join()
    line = _latency_lines(harness)[0]
    lock_wait = float(line.split("lock_wait=")[1].split()[0])
    assert lock_wait >= 140
