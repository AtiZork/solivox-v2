"""Pure sniper sell decision helpers (no DB/RPC imports)."""

from __future__ import annotations


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
    if price_tracking_map is None:
        price_tracking_map = {}

    amount = trade_data.purchased_token_amount or 0
    initial_price = trade_data.initial_price or 0
    if initial_price <= 0 or amount <= 0 or not current_price:
        return 0, None

    profit_multiplier = current_price / initial_price
    drop_cutoff_on = sniper_flag_enabled(trade_data, "drop_cutoff_enabled")
    drop_after_100_on = sniper_flag_enabled(trade_data, "drop_after_100_enabled")
    drop_after_400_on = sniper_flag_enabled(trade_data, "drop_after_400_enabled")

    # FIELD_3: sell if drop from buy price
    if drop_cutoff_on and current_price < initial_price * ((100 - trade_data.drop_cutoff) / 100):
        return amount, f"Auto-Sell All: Drops below {trade_data.drop_cutoff}%"

    if profit_multiplier * 100 >= trade_data.drop_until_profit:
        return 0, f"Profit reached limit of {trade_data.drop_until_profit}%, skipping further sells."

    if profit_multiplier >= 2.0:
        peak_price = price_tracking_map.get(trade_data.id, current_price)
        price_tracking_map[trade_data.id] = max(peak_price, current_price)
        drop_percent = 100 * (peak_price - current_price) / peak_price if peak_price else 0

        # FIELD_1: after 100% profit, sell if drops
        if drop_after_100_on and drop_percent >= trade_data.drop_after_100:
            return amount, f"Auto-Sell All after 100% profit, dropped {drop_percent:.2f}%"

        # FIELD_2: after 400% profit, sell if drops
        if (
            drop_after_400_on
            and profit_multiplier >= 5.0
            and drop_percent >= trade_data.drop_after_400
        ):
            return amount, f"Auto-Sell All after 400% profit, dropped {drop_percent:.2f}%"

    # Profit-target partial sells (highest threshold first).
    # 100% profit => 2x price; 200% uses existing <=3.0 check; 400% => 5x.
    def _partial(pct, label):
        pct = pct or 0
        if pct <= 0:
            return None
        return amount * (pct / 100), f"Auto-Sell {pct}% at {label} Profit"

    sell_at_100 = getattr(trade_data, "sell_at_100", 10)
    for threshold, pct, label in (
        (101.0, trade_data.sell_at_10000, "10000%"),
        (41.0, trade_data.sell_at_4000, "4000%"),
        (26.0, trade_data.sell_at_2500, "2500%"),
        (16.0, trade_data.sell_at_1500, "1500%"),
        (11.0, trade_data.sell_at_1000, "1000%"),
        (5.0, trade_data.sell_at_400, "400%"),
        (2.0, sell_at_100, "100%"),
    ):
        if profit_multiplier >= threshold:
            result = _partial(pct, label)
            if result is not None:
                return result
            # pct is 0 for this tier — keep checking lower tiers
            continue

    if profit_multiplier <= 3.0:
        result = _partial(trade_data.sell_at_200, "200%")
        if result is not None:
            return result
    return 0, None
