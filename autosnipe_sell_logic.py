"""Pure sniper sell decision helpers (no DB/RPC imports)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# (price multiplier threshold, Trade attribute holding the % to sell, label).
# N% profit == price at (1 + N/100)x the buy price. Each tier's % is a share of
# the original position, so the tiers add up as the price climbs.
TAKE_PROFIT_TIERS = (
    (2.0, "sell_at_100", "100%"),
    (3.0, "sell_at_200", "200%"),
    (5.0, "sell_at_400", "400%"),
    (11.0, "sell_at_1000", "1000%"),
    (16.0, "sell_at_1500", "1500%"),
    (26.0, "sell_at_2500", "2500%"),
    (41.0, "sell_at_4000", "4000%"),
    (101.0, "sell_at_10000", "10000%"),
)


@dataclass
class SellDecision:
    amount: float = 0
    message: Optional[str] = None
    # Set only for take-profit sells. After a successful send the caller
    # persists take_profit_pct as Trade.tp_pct_sold (cumulative % of the
    # original position sold by take-profit) and take_profit_tier as
    # Trade.last_tp_tier (highest tier reached), so no tier is sold twice.
    take_profit_tier: Optional[float] = None
    take_profit_pct: Optional[float] = None


def sniper_flag_enabled(trade_data, attr_name: str) -> bool:
    """Enable flags default to True so older trades keep existing sell behavior."""
    value = getattr(trade_data, attr_name, True)
    if value is None:
        return True
    return bool(value)


def evaluate_autosnipe_sell_amount(trade_data, current_price, price_tracking_map=None):
    """
    Decide sniper sell amount/message for a trade at current_price.

    Returns (amount_to_trade, message). amount_to_trade 0 means skip.
    """
    decision = evaluate_autosnipe_sell(trade_data, current_price, price_tracking_map)
    return decision.amount, decision.message


def evaluate_autosnipe_sell(trade_data, current_price, price_tracking_map=None) -> SellDecision:
    if price_tracking_map is None:
        price_tracking_map = {}

    amount = trade_data.purchased_token_amount or 0
    initial_price = trade_data.initial_price or 0
    if initial_price <= 0 or amount <= 0 or not current_price:
        return SellDecision()

    profit_multiplier = current_price / initial_price
    drop_cutoff_on = sniper_flag_enabled(trade_data, "drop_cutoff_enabled")
    drop_after_100_on = sniper_flag_enabled(trade_data, "drop_after_100_enabled")
    drop_after_400_on = sniper_flag_enabled(trade_data, "drop_after_400_enabled")

    # FIELD_3: sell if drop from buy price
    if drop_cutoff_on and current_price < initial_price * ((100 - trade_data.drop_cutoff) / 100):
        return SellDecision(amount, f"Auto-Sell All: Drops below {trade_data.drop_cutoff}%")

    # drop_until_profit is intentionally not a gate here. It used to return
    # "skip" whenever profit_multiplier * 100 >= drop_until_profit; with the
    # default of 99 that meant any price >= 0.99x the buy price, so every
    # take-profit and trailing-stop rule below was unreachable.

    if profit_multiplier >= 2.0:
        peak_price = price_tracking_map.get(trade_data.id, current_price)
        price_tracking_map[trade_data.id] = max(peak_price, current_price)
        drop_percent = 100 * (peak_price - current_price) / peak_price if peak_price else 0

        # FIELD_1: after 100% profit, sell if drops
        if drop_after_100_on and drop_percent >= trade_data.drop_after_100:
            return SellDecision(amount, f"Auto-Sell All after 100% profit, dropped {drop_percent:.2f}%")

        # FIELD_2: after 400% profit, sell if drops
        if (
            drop_after_400_on
            and profit_multiplier >= 5.0
            and drop_percent >= trade_data.drop_after_400
        ):
            return SellDecision(amount, f"Auto-Sell All after 400% profit, dropped {drop_percent:.2f}%")

    # Take-profit: every tier reached sells its % of the original position
    # once. Sell the gap between what all reached tiers add up to and what has
    # already been sold, so a price that jumps past several tiers sells all of
    # them in one go, and a tier is never sold twice.
    reached = []
    target_pct = 0.0
    for threshold, attr, label in TAKE_PROFIT_TIERS:
        if profit_multiplier < threshold:
            break
        default = 10 if attr == "sell_at_100" else 0
        pct = getattr(trade_data, attr, default) or 0
        if pct > 0:
            target_pct += pct
            reached.append((threshold, label))
    target_pct = min(target_pct, 100.0)
    sold_pct = getattr(trade_data, "tp_pct_sold", 0) or 0
    if reached and target_pct > sold_pct:
        original = getattr(trade_data, "initial_token_amount", None) or amount
        sell_pct = target_pct - sold_pct
        sell_amount = min(original * sell_pct / 100, amount)
        return SellDecision(
            sell_amount,
            f"Auto-Sell {sell_pct:g}% of position at {reached[-1][1]} Profit (take-profit total {target_pct:g}%)",
            reached[-1][0],
            target_pct,
        )

    return SellDecision()
