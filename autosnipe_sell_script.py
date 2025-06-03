import logging
import os
import atexit
import time

from dotenv import load_dotenv
from models import db, Wallet, Trade, TradeLog, TradeHistory
import requests
import base64
from flask import jsonify, Blueprint
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from settings import solana_client
from utils import get_token_symbol_and_price, get_token_metadata
from solders.pubkey import Pubkey
from apscheduler.schedulers.background import BackgroundScheduler

log_messages = []

# Custom log handler to store logs
class ListLogHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        log_messages.append(log_entry)  # Store logs
        if len(log_messages) > 100:  # Keep only last 100 logs
            log_messages.pop(0)


# Custom logging handler to store logs in DB
class DBLogHandler(logging.Handler):
    def emit(self, record):
        log_entry = TradeLog(level=record.levelname, message=self.format(record))
        db.session.add(log_entry)
        db.session.commit()


# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
db_handler = DBLogHandler()
db_handler.setLevel(logging.INFO)
logger.addHandler(db_handler)

logging.basicConfig()
logging.getLogger("apscheduler").setLevel(logging.DEBUG)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
API_KEY = os.getenv("API_KEY")
# Free tier users should use lite-api.jup.ag. api.jup.ag is for paid plans and requires an API key
API_BASE_URL = "https://api.jup.ag" if API_KEY else "https://lite-api.jup.ag"
# Set up headers for API requests (include x-api-key if API_KEY is available)
headers = {"x-api-key": API_KEY} if API_KEY else {}
sell_trade_bp = Blueprint('sell_trade_bp', __name__)

price_tracking = {}

def auto_snipe_auto_sell_schedular(app):
    scheduler = BackgroundScheduler(daemon=True)
    # Auto-snipe logic to sell tokens based on configurable conditions
    def auto_snipe_sell():
        with app.app_context():
            try:
                """Handles the auto-snipe logic for selling tokens based on trade settings."""
                # Fetch trades that have not been executed
                trades = Trade.query.filter_by(executed=False, auto_snipe=True).order_by(Trade.id.desc()).all()
                for trade_data in trades:
                    # Fetch the price of the token associated with the trade
                    current_price_ = get_token_symbol_and_price(trade_data.token_address)
                    current_price = current_price_['usdPrice']
                    amount = trade_data.purchased_token_amount
                    initial_price = trade_data.initial_price
                    if initial_price <= 0:
                        logger.warning(f"Invalid initial price for trade {trade_data.id}. Skipping auto-snipe.")
                        continue

                    profit_multiplier = current_price / initial_price
                    # --- NEW: Sell all if drops below %
                    if current_price < initial_price * ((100 - trade_data.drop_cutoff) / 100):
                        amount_to_trade = amount
                        message = f"Auto-Sell All: Drops below {trade_data.drop_cutoff}%"
                    # --- NEW: Stop if profit target met
                    elif profit_multiplier * 100 >= trade_data.drop_until_profit:
                        logger.info(
                            f"Profit reached limit of {trade_data.drop_until_profit}%, skipping further sells.")
                        continue
                    # --- NEW: After 100% profit, sell all if drops %
                    elif profit_multiplier >= 2.0:
                        peak_price = price_tracking.get(trade_data.id, current_price)
                        price_tracking[trade_data.id] = max(peak_price, current_price)
                        drop_percent = 100 * (peak_price - current_price) / peak_price
                        if drop_percent >= trade_data.drop_after_100:
                            amount_to_trade = amount
                            message = f"Auto-Sell All after 100% profit, dropped {drop_percent:.2f}%"
                        # --- NEW: After 400% profit, sell all if drops %
                        elif profit_multiplier >= 5.0 and drop_percent >= trade_data.drop_after_400:
                            amount_to_trade = amount
                            message = f"Auto-Sell All after 400% profit, dropped {drop_percent:.2f}%"
                        else:
                            amount_to_trade = 0  # prevent accidental sell

                    # Track peak and drop conditions
                    elif profit_multiplier <= 3.0:
                        amount_to_trade = amount * (trade_data.sell_at_200 / 100)
                        message = f"Auto-Sell {trade_data.sell_at_200}% at 200% Profit"
                    elif profit_multiplier >= 5.0:
                        amount_to_trade = amount * (trade_data.sell_at_400 / 100)
                        message = f"Auto-Sell {trade_data.sell_at_400}% at 400% Profit"
                    elif profit_multiplier >= 11.0:
                        amount_to_trade = amount * (trade_data.sell_at_1000 / 100)
                        message = f"Auto-Sell {trade_data.sell_at_1000}% at 1000% Profit"
                    elif profit_multiplier >= 16.0:
                        amount_to_trade = amount * (trade_data.sell_at_1500 / 100)
                        message = f"Auto-Sell {trade_data.sell_at_1500}% at 1500% Profit"
                    elif profit_multiplier >= 26.0:
                        amount_to_trade = amount * (trade_data.sell_at_2500 / 100)
                        message = f"Auto-Sell {trade_data.sell_at_2500}% at 2500% Profit"
                    elif profit_multiplier >= 41.0:
                        amount_to_trade = amount * (trade_data.sell_at_4000 / 100)
                        message = f"Auto-Sell {trade_data.sell_at_4000}% at 4000% Profit"
                    elif profit_multiplier >= 101.0:
                        amount_to_trade = amount * (trade_data.sell_at_10000 / 100)
                        message = f"Auto-Sell {trade_data.sell_at_10000}% at 10000% Profit"
                    else:
                        continue  # No selling condition met

                    if amount_to_trade > 0:
                        try:
                            # Continue with the same logic for performing the trade
                            wallet = Wallet.query.filter_by(public_key=trade_data.to_pubkey).first()
                            if not wallet:
                                logger.warning(f"Wallet not found for {trade_data.to_pubkey}, skipping trade.")
                                continue
                            private_key_path = wallet.private_key
                            if not os.path.exists(private_key_path):
                                return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

                            with open(private_key_path, 'rb') as key_file:
                                private_key_bytes = key_file.read()
                                # Create a solders Keypair directly from the private key bytes
                            wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)

                            metadata = get_token_metadata(trade_data.token_address)
                            if metadata:
                                decimals = metadata.get("decimals", 0)
                                print(f"Token has {decimals} decimals")
                            else:
                                decimals = 0

                            amount_in_lamports = int(amount_to_trade * (10 ** decimals))

                            # Fetch quote for the trade
                            quote_params = {
                                "inputMint": trade_data.token_address,
                                "outputMint": "So11111111111111111111111111111111111111112",
                                "amount": amount_in_lamports,  # amount in lamports (SOL)
                                "slippageBps": int(trade_data.autosnipe_sell_slippage * 100)
                            }

                            quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
                            quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)
                            if quote_response.status_code != 200:
                                logger.error(f"Error fetching quote: {quote_response.json()}")
                                continue

                            quote_data = quote_response.json()
                            logger.info(f"Quote data: {quote_data}")

                            # Fetch the swap transaction for the quote
                            swap_request = {
                                "userPublicKey": str(wallet_keypair.pubkey()),
                                "quoteResponse": quote_data,
                                "computeUnitPriceMicroLamports": int(trade_data.default_gas_fee * 1_000_000),
                                "wrapUnwrapSOL": True  # Automatically handle SOL to WSOL conversion
                            }

                            swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
                            swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers)
                            if swap_response.status_code != 200:
                                logger.error(f"Error performing swap: {swap_response.json()}")
                                continue

                            swap_data = swap_response.json()
                            swap_transaction_base64 = swap_data["swapTransaction"]
                            swap_transaction_bytes = base64.b64decode(swap_transaction_base64)
                            raw_transaction = VersionedTransaction.from_bytes(swap_transaction_bytes)

                            # Sign the transaction
                            account_keys = raw_transaction.message.account_keys
                            wallet_index = account_keys.index(wallet_keypair.pubkey())
                            signers = list(raw_transaction.signatures)
                            signers[wallet_index] = wallet_keypair
                            signed_transaction = VersionedTransaction(raw_transaction.message, signers)

                            # Send the signed transaction
                            try:
                                rpc_response = solana_client.send_transaction(signed_transaction)
                                signature = str(rpc_response.value)
                                logger.info(f"{message}View transaction on Solscan: https://solscan.io/tx/{signature}")
                                logger.info(f"Transaction sent successfully! Signature: {signature}")
                                print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")

                                # Store executed trade in database
                                executed_trade = TradeHistory(
                                    trade_id=trade_data.id,
                                    token_address=trade_data.token_address,
                                    trade_type="SELL",
                                    trade_kind="AutoSnipe",
                                    amount=amount_to_trade,
                                    execution_price=current_price if current_price else 0,
                                    tx_id=signature
                                )
                                db.session.add(executed_trade)
                                db.session.commit()
                                # Update trade data
                                trade_data.purchased_token_amount -= amount_to_trade
                                if trade_data.purchased_token_amount <= 0:
                                    trade_data.executed = True  # Soft delete
                                db.session.commit()
                            except Exception as e:
                                logger.error(f"Error sending transaction: {str(e)}")

                        except Exception as e:
                            logger.error(f"Auto-snipe sell error: {str(e)}")
                            db.session.rollback()
                print("autosnipe token sell successfully executed")
                return None
            except Exception as e:
                db.session.rollback()
                logger.error(f"Unexpected error in auto_trade loop: {e}")
                time.sleep(1)

    # Start background job
    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_snipe_sell, 'interval', seconds=5000)  # Run auto_trade every 60 seconds (or adjust as needed)
    scheduler.start()
    # To ensure the scheduler shuts down gracefully when the app exits
    atexit.register(lambda: scheduler.shutdown())