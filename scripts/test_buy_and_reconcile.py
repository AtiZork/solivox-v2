"""
Standalone end-to-end test: buy a real token directly (bypassing the
sniper's detection/eligibility check), wait for on-chain confirmation, then
run the entry-price reconciliation immediately (not waiting for the
scheduled job) and print the result - so whether the price actually gets
saved correctly can be checked in one run, independent of the live bot.

THIS SPENDS REAL SOL (buy_amount from your AutoSnipeConfig). Only run it
against a token you intend to actually buy.

Usage (run from the project root, same Python environment as the service):
  python scripts/test_buy_and_reconcile.py <mint>
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from solders.signature import Signature

from config import Config
from models import db, AutoSnipeConfig, Trade, TradeHistory


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_buy_and_reconcile.py <mint>")
        return 1
    mint = sys.argv[1]

    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        from autosnipe_buy_new_token import buy_token, reconcile_pending_buys, _actual_tokens_received, get_sol_usd_rate
        from settings import solana_client

        config = AutoSnipeConfig.query.filter_by(active=True).first()
        if not config:
            print("No active AutoSnipeConfig found - cannot proceed.")
            return 1
        print(f"Using AutoSnipeConfig id={config.id}, user_id={config.user_id}, buy_amount={config.buy_amount} SOL")

        print(f"\n--- Buying {mint} ---")
        buy_token(mint, config)

        trade = Trade.query.filter_by(token_address=mint).order_by(Trade.id.desc()).first()
        if not trade:
            print("No Trade row was created - buy_token() did not record a trade (check logs above for why).")
            return 1
        print(f"Trade id={trade.id} created. amount={trade.amount} SOL, "
              f"purchased_token_amount={trade.purchased_token_amount} (provisional quote estimate), "
              f"initial_price={trade.initial_price} (should be 0.0 placeholder right now)")

        history = TradeHistory.query.filter_by(trade_id=trade.id, trade_type="BUY").order_by(TradeHistory.id.desc()).first()
        if not history:
            print("No TradeHistory row found for this trade.")
            return 1
        print(f"TradeHistory id={history.id}, status={history.status}, tx_id={history.tx_id}")

        print("\n--- Waiting for on-chain confirmation (up to 30s) ---")
        deadline = time.monotonic() + 30
        confirmed = False
        while time.monotonic() < deadline:
            resp = solana_client.get_signature_statuses([Signature.from_string(history.tx_id)])
            status = (resp.value or [None])[0]
            if status is not None:
                print(f"  status: err={status.err}, confirmation_status={status.confirmation_status}")
                if status.err is None and status.confirmation_status is not None:
                    confirmed = True
                    break
                if status.err is not None:
                    print("  Transaction FAILED on-chain.")
                    break
            time.sleep(2)

        if not confirmed:
            print("Transaction did not confirm within 30s - reconciliation will catch it on a later run.")
            return 1

        print("\n--- Running reconciliation now (not waiting for the scheduled job) ---")
        reconcile_pending_buys(app)

        db.session.refresh(trade)
        db.session.refresh(history)
        print(f"\nAfter reconciliation:")
        print(f"  TradeHistory.status:        {history.status}")
        print(f"  TradeHistory.execution_price: {history.execution_price}")
        print(f"  Trade.initial_price:        {trade.initial_price}")
        print(f"  Trade.purchased_token_amount: {trade.purchased_token_amount}")

        print("\n--- Independent re-derivation, for comparison ---")
        tx_resp = solana_client.get_transaction(
            Signature.from_string(history.tx_id), encoding='jsonParsed', max_supported_transaction_version=0,
        )
        actual_tokens = _actual_tokens_received(tx_resp.value, trade.token_address, trade.to_pubkey) if tx_resp.value else None
        sol_usd = get_sol_usd_rate()
        print(f"  Actual tokens received (re-parsed): {actual_tokens}")
        print(f"  SOL/USD rate (re-fetched):           {sol_usd}")
        if actual_tokens and sol_usd:
            expected_price = (trade.amount * sol_usd) / actual_tokens
            print(f"  Expected entry price:                {expected_price}")
            match = abs(expected_price - (trade.initial_price or 0)) < 1e-12
            print(f"  Matches Trade.initial_price:         {match}")

        if trade.initial_price and trade.initial_price > 0:
            print("\nRESULT: PASS - entry price was correctly computed and saved.")
            return 0
        print("\nRESULT: FAIL - Trade.initial_price is still 0.0 after reconciliation ran.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
