"""
Diagnose why specific open Sniper trades haven't sold yet.

Runs the exact same price-fetch + sell-decision code the live scheduler uses
(get_sniper_sell_price + evaluate_autosnipe_sell_amount) against real open
trades from the production DB, and prints exactly why each one has/hasn't
triggered a sell yet (profit not past a configured threshold, price fetch
failing, etc.) instead of guessing from logs.

Run on the server, from the project root, using the same Python environment
the "solivox" service uses:
  python scripts/diagnose_stuck_sells.py                 # all open auto-snipe trades
  python scripts/diagnose_stuck_sells.py <mint1> <mint2>  # only these mints
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask

from config import Config
from models import db, Trade
from autosnipe_sell_logic import evaluate_autosnipe_sell_amount
from autosnipe_sell_script import get_sniper_sell_price


def main() -> int:
    requested_mints = set(sys.argv[1:])

    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    price_tracking: dict = {}

    with app.app_context():
        query = Trade.query.filter_by(executed=False, auto_snipe=True)
        if requested_mints:
            query = query.filter(Trade.token_address.in_(requested_mints))
        trades = query.order_by(Trade.id.desc()).all()

        if not trades:
            print("No matching open auto-snipe trades found.")
            return 0

        for trade_data in trades:
            print(f"\n=== Trade #{trade_data.id} | {trade_data.token_name or ''} "
                  f"({trade_data.token_address}) ===")
            print(f"  initial_price      = {trade_data.initial_price}")
            print(f"  drop_cutoff        = {trade_data.drop_cutoff}%  "
                  f"(enabled={getattr(trade_data, 'drop_cutoff_enabled', True)})")
            print(f"  drop_until_profit  = {trade_data.drop_until_profit}%")
            print(f"  sell_at_100/200/400/1000/1500/2500/4000/10000 = "
                  f"{getattr(trade_data, 'sell_at_100', None)}/"
                  f"{trade_data.sell_at_200}/{trade_data.sell_at_400}/"
                  f"{trade_data.sell_at_1000}/{trade_data.sell_at_1500}/"
                  f"{trade_data.sell_at_2500}/{trade_data.sell_at_4000}/"
                  f"{trade_data.sell_at_10000}  (% of holdings sold at each profit tier)")

            try:
                price_data = get_sniper_sell_price(trade_data.token_address)
                current_price = price_data.get("usdPrice")
            except Exception as exc:
                print(f"  [PRICE FETCH FAILED] {exc!r}")
                continue

            if not current_price or current_price <= 0:
                print(f"  [PRICE FETCH] returned no usable price: {price_data!r}")
                continue

            profit_multiplier = (
                current_price / trade_data.initial_price if trade_data.initial_price else 0
            )
            print(f"  current_price      = {current_price}  "
                  f"(source={price_data.get('source')})")
            print(f"  profit_multiplier  = {profit_multiplier:.3f}x "
                  f"({(profit_multiplier - 1) * 100:.1f}% since buy)")

            amount_to_trade, message = evaluate_autosnipe_sell_amount(
                trade_data, current_price, price_tracking
            )
            if amount_to_trade > 0:
                print(f"  [DECISION] WOULD SELL {amount_to_trade} tokens — {message}")
            elif message:
                print(f"  [DECISION] no sell — {message}")
            else:
                print(
                    "  [DECISION] no sell — profit/drop hasn't crossed any configured "
                    "threshold yet (this is expected behavior, not a bug, if none of "
                    "the percentages above have been reached)"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
