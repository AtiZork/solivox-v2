"""
Buy detection must recognize a genuine buy regardless of which program
routed the swap (direct Pump.fun bonding curve vs. Jupiter/Orca/Raydium/any
other aggregator or AMM), and must decide buy-vs-sell from the transaction's
own balance changes - never from the instruction name alone. See
sniper_stream._resolve_signer_buy / _net_mint_delta / _account_key_str /
_process_mint_buy.

Root cause this replaces: the old detector only counted a transaction whose
logs contained the literal "Instruction: Buy" (Pump.fun's own bonding-curve
instruction). Any trade routed through an aggregator - which logs generic
names like "Instruction: Swap" - was invisible to the counter regardless of
real volume, confirmed live against a real on-chain transaction.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import sniper_stream as ss

MINT = "TargetMint1111111111111111111111111111pump"
SIGNER = "Signer1111111111111111111111111111111111"
WSOL = ss.WSOL_MINT
INTERMEDIATE_MINT = "IntermediateMint111111111111111111111111"


def _bal(owner, mint, ui_amount):
    return SimpleNamespace(owner=owner, mint=mint, ui_token_amount=SimpleNamespace(ui_amount=ui_amount))


def _key(pubkey_str, parsed=False):
    """Plain Pubkey-like (str() -> address) or a jsonParsed-style wrapper
    (pubkey/writable/signer/source), matching what _get_transaction_with_retry
    actually requests (encoding="jsonParsed") in production."""
    return SimpleNamespace(pubkey=pubkey_str) if parsed else pubkey_str


def _tx(account_keys, pre_token=None, post_token=None, pre_balances=None, post_balances=None, err=None):
    meta = SimpleNamespace(
        err=err,
        pre_token_balances=pre_token or [],
        post_token_balances=post_token or [],
        pre_balances=pre_balances or [0] * len(account_keys),
        post_balances=post_balances or [0] * len(account_keys),
    )
    return SimpleNamespace(
        transaction=SimpleNamespace(
            meta=meta,
            transaction=SimpleNamespace(message=SimpleNamespace(account_keys=account_keys)),
        )
    )


@pytest.fixture
def stats():
    fresh = ss.StageStats("test", interval_sec=0)
    with patch.object(ss, "rpc_stats", fresh):
        yield fresh


# --- Regression: the exact bug just found/fixed in jsonParsed account_keys ---

def test_account_key_str_unwraps_parsed_account_wrapper():
    assert ss._account_key_str(_key(SIGNER, parsed=True)) == SIGNER


def test_account_key_str_passes_through_plain_pubkey_like():
    assert ss._account_key_str(_key(SIGNER, parsed=False)) == SIGNER


# --- 1. Direct Pump.fun buys ---

def test_direct_pumpfun_buy_resolved_from_native_sol_delta(stats):
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        post_token=[_bal(SIGNER, MINT, 1000.0)],
        pre_balances=[5_000_000_000],
        post_balances=[4_955_000_000],
    )
    assert ss._resolve_signer_buy(tx, MINT) == pytest.approx(0.045)


# --- 2. Jupiter-routed buys ---

def test_jupiter_routed_buy_resolved_from_wsol_delta(stats):
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        pre_token=[_bal(SIGNER, WSOL, 1.0)],
        post_token=[_bal(SIGNER, WSOL, 0.9), _bal(SIGNER, MINT, 5000.0)],
        pre_balances=[5_000_000_000],
        post_balances=[4_999_000_000],  # fee only - real cost is the WSOL delta, not lamports
    )
    assert ss._resolve_signer_buy(tx, MINT) == pytest.approx(0.1)


def test_old_instruction_name_check_would_have_missed_this_buy():
    """The exact gap this fix closes: a real aggregator-routed buy whose
    logs never contain Pump.fun's own "Instruction: Buy" string."""
    logs = ["Program log: Instruction: Route", "Program log: Instruction: Swap"]
    assert not ss._logs_contain(logs, "Instruction: Buy")
    assert any(ss._logs_contain(logs, m) for m in ss._SWAP_LOG_MARKERS)


# --- 3. Raydium/Orca/other AMM-routed buys ---

def test_amm_routed_buy_resolved_from_wsol_delta_regardless_of_program(stats):
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        pre_token=[_bal(SIGNER, WSOL, 2.0)],
        post_token=[_bal(SIGNER, WSOL, 1.8), _bal(SIGNER, MINT, 12000.0)],
        pre_balances=[3_000_000_000],
        post_balances=[2_999_000_000],
    )
    assert ss._resolve_signer_buy(tx, MINT) == pytest.approx(0.2)


# --- 4. Aggregator-routed sells must never be counted as buys ---

def test_aggregator_routed_sell_is_excluded_not_counted_as_buy(stats):
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        pre_token=[_bal(SIGNER, MINT, 5000.0)],
        post_token=[_bal(SIGNER, MINT, 0.0), _bal(SIGNER, WSOL, 0.3)],
        pre_balances=[3_000_000_000],
        post_balances=[3_000_900_000],
    )
    assert ss._resolve_signer_buy(tx, MINT) is None
    assert stats.snapshot()["counts"]["buy_excluded_not_signer_gain"] == 1


# --- 5. Multi-hop swaps and wrapped SOL ---

def test_multi_hop_route_only_counts_net_wsol_cost_and_target_mint_gain(stats):
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        pre_token=[_bal(SIGNER, WSOL, 1.0), _bal(SIGNER, INTERMEDIATE_MINT, 0.0)],
        post_token=[
            _bal(SIGNER, WSOL, 0.85),
            _bal(SIGNER, INTERMEDIATE_MINT, 0.0),  # passed through a hop, net zero - must not count
            _bal(SIGNER, MINT, 8800.0),
        ],
        pre_balances=[4_000_000_000],
        post_balances=[3_999_000_000],
    )
    assert ss._resolve_signer_buy(tx, MINT) == pytest.approx(0.15)


# --- 6. Multiple token accounts for the same mint ---

def test_net_mint_delta_sums_across_multiple_accounts_for_same_mint():
    meta = SimpleNamespace(
        pre_token_balances=[_bal(SIGNER, MINT, 100.0)],
        post_token_balances=[_bal(SIGNER, MINT, 100.0), _bal(SIGNER, MINT, 50.0)],
    )
    assert ss._net_mint_delta(meta, SIGNER, MINT) == pytest.approx(50.0)


# --- 7. Failed transactions, missing cost signal, unavailable details ---

def test_failed_onchain_transaction_excluded(stats):
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        post_token=[_bal(SIGNER, MINT, 1000.0)],
        pre_balances=[1_000_000_000],
        post_balances=[950_000_000],
        err={"InstructionError": [0, "Custom"]},
    )
    assert ss._resolve_signer_buy(tx, MINT) is None
    assert stats.snapshot()["counts"]["buy_excluded_failed_onchain"] == 1


def test_transaction_unavailable_returns_none_without_raising():
    assert ss._resolve_signer_buy(None, MINT) is None


def test_missing_cost_signal_excluded_not_guessed(stats):
    """Signer gained the mint but no SOL/WSOL movement is visible (paid in
    some other token entirely, or data is incomplete) - never counted on a
    guess."""
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        post_token=[_bal(SIGNER, MINT, 1000.0)],
        pre_balances=[1_000_000_000],
        post_balances=[1_000_000_000],
    )
    assert ss._resolve_signer_buy(tx, MINT) is None
    assert stats.snapshot()["counts"]["buy_excluded_no_cost_found"] == 1


def test_unresolvable_candidate_does_not_block_later_signatures_for_same_mint():
    """A technical exclusion (tx unavailable) for one signature must not
    prevent a later, genuinely good signature on the same mint from being
    counted."""
    state = ss.SniperStreamState()
    state.register_launch(MINT, "create-sig")
    good_tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        post_token=[_bal(SIGNER, MINT, 1000.0)],
        pre_balances=[1_000_000_000],
        post_balances=[955_000_000],
    )
    with patch.object(ss, "stream_state", state), patch.object(ss, "_get_sol_usd_price", return_value=150.0):
        with patch.object(ss, "_get_transaction_with_retry", return_value=None):
            ss._process_mint_buy(MINT, "sig-unavailable", ["Program log: Instruction: Swap"])
        with patch.object(ss, "_get_transaction_with_retry", return_value=good_tx):
            ss._process_mint_buy(MINT, "sig-good", ["Program log: Instruction: Swap"])
    buy_stats = state.get_buy_stats(MINT, min_usd_value=0)
    assert buy_stats["total_buys"] == 1
    assert buy_stats["candidates_seen"] == 2
    assert buy_stats["candidates_excluded"] == 1


# --- 8. Duplicate events and concurrent processing ---

def test_duplicate_signature_is_only_counted_once():
    state = ss.SniperStreamState()
    state.register_launch(MINT, "create-sig")
    with patch.object(ss, "stream_state", state), \
         patch.object(ss, "_resolve_buy_amount", return_value=0.05), \
         patch.object(ss, "_get_sol_usd_price", return_value=150.0):
        ss._process_mint_buy(MINT, "dup-sig", ["Program log: Instruction: Swap"])
        ss._process_mint_buy(MINT, "dup-sig", ["Program log: Instruction: Swap"])
    buy_stats = state.get_buy_stats(MINT, min_usd_value=0)
    assert buy_stats["total_buys"] == 1


def test_concurrent_buys_on_same_mint_are_all_counted_thread_safely():
    state = ss.SniperStreamState()
    state.register_launch(MINT, "create-sig")
    with patch.object(ss, "stream_state", state), \
         patch.object(ss, "_resolve_buy_amount", return_value=0.01), \
         patch.object(ss, "_get_sol_usd_price", return_value=150.0):
        threads = [
            threading.Thread(
                target=ss._process_mint_buy,
                args=(MINT, f"sig-{i}", ["Program log: Instruction: Swap"]),
            )
            for i in range(25)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    buy_stats = state.get_buy_stats(MINT, min_usd_value=0)
    assert buy_stats["total_buys"] == 25


# --- 9. Tokens that genuinely fail thresholds (not a detection gap) ---

def test_genuinely_no_cost_candidate_stays_excluded_and_is_visible_as_such():
    state = ss.SniperStreamState()
    state.register_launch(MINT, "create-sig")
    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        post_token=[_bal(SIGNER, MINT, 1000.0)],
        pre_balances=[1_000_000_000],
        post_balances=[1_000_000_000],
    )
    with patch.object(ss, "stream_state", state), patch.object(ss, "_get_transaction_with_retry", return_value=tx):
        ss._process_mint_buy(MINT, "sig-no-cost", ["Program log: Instruction: Swap"])
    buy_stats = state.get_buy_stats(MINT, min_usd_value=0)
    assert buy_stats["total_buys"] == 0
    assert buy_stats["candidates_seen"] == 1
    assert buy_stats["candidates_excluded"] == 1


# --- 10. Tokens that meet thresholds but previously would have been missed ---

def test_hot_mint_stops_spending_rpc_once_past_candidate_ceiling():
    """A single very high-volume mint must not keep burning RPC calls (and
    starving the shared throttle/pool that other mints' detection also
    depends on) forever - once it has far more candidates than any
    realistic min_txns threshold needs, further signatures are skipped
    without an RPC call, not silently miscounted."""
    state = ss.SniperStreamState()
    state.register_launch(MINT, "create-sig")
    tracker = state.mint_registry[MINT]
    tracker.candidates_seen = ss.SNIPER_MAX_BUY_CANDIDATES_PER_MINT

    calls = {"n": 0}

    def _counting_get_transaction(signature, *, max_attempts=3):
        calls["n"] += 1
        return None

    with patch.object(ss, "stream_state", state), \
         patch.object(ss, "_get_transaction_with_retry", _counting_get_transaction):
        ss._process_mint_buy(MINT, "sig-over-ceiling", ["Program log: Instruction: Swap"])

    assert calls["n"] == 0  # no RPC call was made at all
    assert tracker.candidates_seen == ss.SNIPER_MAX_BUY_CANDIDATES_PER_MINT  # not incremented further either


def test_previously_missed_aggregator_buy_is_now_counted_end_to_end():
    """Full old-vs-new regression: a real-shaped Jupiter buy whose logs
    never say "Instruction: Buy" is now detected, counted, and reflected in
    get_buy_stats - the exact gap reported live in production (tokens with
    real volume still showing "Buy conditions NOT MET")."""
    state = ss.SniperStreamState()
    state.register_launch(MINT, "create-sig")
    logs = ["Program log: Instruction: Route", "Program log: Instruction: Swap"]
    assert not ss._logs_contain(logs, "Instruction: Buy")  # old gate would have silently dropped this

    tx = _tx(
        account_keys=[_key(SIGNER, parsed=True)],
        pre_token=[_bal(SIGNER, WSOL, 1.0)],
        post_token=[_bal(SIGNER, WSOL, 0.95), _bal(SIGNER, MINT, 3000.0)],
        pre_balances=[2_000_000_000],
        post_balances=[1_999_000_000],
    )
    with patch.object(ss, "stream_state", state), \
         patch.object(ss, "_get_transaction_with_retry", return_value=tx), \
         patch.object(ss, "_get_sol_usd_price", return_value=150.0):
        ss._process_mint_buy(MINT, "jupiter-sig", logs)

    buy_stats = state.get_buy_stats(MINT, min_usd_value=0)
    assert buy_stats["total_buys"] == 1
    assert buy_stats["qualifying_buys"][0].sol_amount == pytest.approx(0.05)
