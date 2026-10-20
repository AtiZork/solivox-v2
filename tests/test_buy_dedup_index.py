"""
Action-plan 1.3: the B10 buy-dedup check (autosnipe_buy_new_token.py) was a
query check only, not a real DB constraint - safe today only because buys
are serialized per wallet (WALLET_BUY_LOCK), not bulletproof against a future
code path that removes that lock. This locks in the partial unique index
Trade.__table_args__ declares, and the in-memory fast-path cleanup that keeps
BOUGHT_TOKENS from shadowing the new open-positions-only dedup scope.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from models import Trade
import autosnipe_buy_new_token as buy


def test_open_autosnipe_dedup_index_is_declared_on_the_model():
    indexes = {ix.name: ix for ix in Trade.__table__.indexes}
    assert "ix_trade_open_autosnipe_user_mint" in indexes
    index = indexes["ix_trade_open_autosnipe_user_mint"]
    assert index.unique is True
    assert [c.name for c in index.columns] == ["user_id", "token_address"]


def test_index_ddl_scopes_to_open_autosnipe_positions_only():
    """Must match the buy-dedup query's own scope (auto_snipe=True,
    executed=False) exactly - a mismatch here would mean the app-level check
    and the DB-level constraint disagree about what counts as a duplicate."""
    index = next(ix for ix in Trade.__table__.indexes if ix.name == "ix_trade_open_autosnipe_user_mint")
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "WHERE auto_snipe = true AND executed = false" in ddl


def test_mark_autosnipe_position_closed_clears_the_in_memory_fast_path():
    buy.BOUGHT_TOKENS.add((7, "MintClosed"))

    buy.mark_autosnipe_position_closed(7, "MintClosed")

    assert (7, "MintClosed") not in buy.BOUGHT_TOKENS


def test_mark_autosnipe_position_closed_is_a_noop_if_never_bought():
    buy.BOUGHT_TOKENS.clear()
    buy.mark_autosnipe_position_closed(7, "NeverBought")  # must not raise
    assert (7, "NeverBought") not in buy.BOUGHT_TOKENS
