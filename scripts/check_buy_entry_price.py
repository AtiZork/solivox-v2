"""
Static test runner: verify the buy-time entry-price calculation
(autosnipe_buy_new_token.py) independent of the live sniper bot.

The entry price itself is computed from actual execution data only - SOL
spent and tokens received, converted via a direct Pyth SOL/USD read
(get_sol_usd_rate(), solana_client, no Shyft, no Geyser) - never fabricated.
A live Geyser price is optionally also shown, purely as an external
reference to compare against, exactly as before; it plays no part in the
calculation itself.

Usage (run from the project root, same Python environment as the service):
  python scripts/check_buy_entry_price.py --trade-id 1245
  python scripts/check_buy_entry_price.py --sol-spent 0.5 --tokens 1000.0
  python scripts/check_buy_entry_price.py --trade-id 1245 --live-mint <mint>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autosnipe_buy_new_token import get_sol_usd_rate


def _print_kv(label: str, value) -> None:
    print(f"  {label:<26} {value}")


def load_trade(trade_id: int):
    from flask import Flask

    from config import Config
    from models import db, Trade

    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    with app.app_context():
        trade = Trade.query.get(trade_id)
        if not trade:
            print(f"  No Trade row found for id={trade_id}.")
            return None
        print(f"\n--- Trade #{trade_id} ---")
        _print_kv("Token address:", trade.token_address)
        _print_kv("SOL spent (amount):", trade.amount)
        _print_kv("Tokens received (purchased_token_amount):", trade.purchased_token_amount)
        _print_kv("Stored initial_price:", trade.initial_price)
        return float(trade.amount or 0.0), float(trade.purchased_token_amount or 0.0), float(trade.initial_price or 0.0)


def fetch_live_reference_price(mint: str) -> None:
    """Optional, informational only - never used in the entry-price
    calculation below."""
    from yellowstone_pricing import YellowstonePricingError, get_yellowstone_price, is_yellowstone_enabled

    print(f"\n--- Live Geyser price for {mint} (reference only) ---")
    if not is_yellowstone_enabled():
        print("  Geyser is DISABLED (USE_GEYSER=false or GEYSER_GRPC_URL unset).")
        return
    try:
        data = get_yellowstone_price(mint)
    except YellowstonePricingError as exc:
        print(f"  FAILED: {exc}")
        return
    _print_kv("Source:", data.get("source"))
    _print_kv("Price (USD):", data.get("usd_price"))
    _print_kv("Timestamp:", data.get("timestamp"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-id", type=int, default=None, help="Show a real Trade row's stored SOL spent/tokens/entry price")
    parser.add_argument("--sol-spent", type=float, default=None, help="Manual: SOL spent (dry run, no DB)")
    parser.add_argument("--tokens", type=float, default=None, help="Manual: tokens received (dry run, no DB)")
    parser.add_argument("--live-mint", default=None, help="Optionally also fetch a live Geyser price for this mint, for reference")
    args = parser.parse_args()

    sol_spent = tokens_received = stored_price = None
    if args.trade_id is not None:
        result = load_trade(args.trade_id)
        if result:
            sol_spent, tokens_received, stored_price = result
    elif args.sol_spent is not None and args.tokens is not None:
        sol_spent, tokens_received = args.sol_spent, args.tokens
    else:
        print("Provide either --trade-id or --sol-spent/--tokens.")
        return 1

    print("\n--- Direct Pyth SOL/USD rate (solana_client, not Shyft, not Geyser) ---")
    sol_usd_rate = get_sol_usd_rate()
    if sol_usd_rate is None:
        print("  FAILED: could not read the Pyth SOL/USD account.")
        return 1
    _print_kv("SOL/USD rate:", sol_usd_rate)

    print("\n--- Entry price ---")
    if stored_price is not None:
        _print_kv("Stored Trade.initial_price:", stored_price)
        entry_price = stored_price
    else:
        if not tokens_received or tokens_received <= 0:
            print("  Could not compute: tokens received is zero/invalid.")
            return 1
        entry_price = (sol_spent * sol_usd_rate) / tokens_received
        _print_kv("Formula:", "(SOL spent * SOL/USD rate) / tokens received")
        _print_kv("SOL spent:", sol_spent)
        _print_kv("Tokens received:", tokens_received)
        _print_kv("Calculated entry price (USD):", entry_price)

    if args.live_mint:
        fetch_live_reference_price(args.live_mint)
        print(
            "\nNote: the live price above is shown for your own independent "
            "comparison only - it is not used anywhere in the entry-price "
            "calculation. The entry price is derived entirely from actual "
            "SOL spent and actual tokens received."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
