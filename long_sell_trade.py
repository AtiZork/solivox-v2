import json
import logging
import atexit
import os
import time
from dotenv import load_dotenv
from models import db, Wallet, Trade, TradeLog, TradeHistory
import requests
import base64
from flask import jsonify, Blueprint
from requests import JSONDecodeError
from solana.rpc.core import RPCException
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


# @sell_trade_bp.route('/auto_trade', methods=['POST'])
def long_auto_sell_schedular(app):
    scheduler = BackgroundScheduler(daemon=True)
    def auto_trade():
        """Automated trading function for Radium token migration."""
        with app.app_context():
            while True:
                try:
                    # trades = Trade.query.all()
                    trades = Trade.query.filter_by(executed=False, auto_snipe=False).order_by(Trade.id.desc()).all()

                    trade_type = "SELL"
                    for trade_data in trades:
                        wallet = Wallet.query.filter_by(public_key=trade_data.to_pubkey).first()
                        if not wallet:
                            logger.warning(f"Wallet not found for {trade_data.to_pubkey}, skipping trade.")
                            continue
                        token_address = trade_data.token_address

                        # Fetch price data
                        current_price_ = get_token_symbol_and_price(token_address)
                        current_price = current_price_['usdPrice']
                        if not current_price:
                            continue

                        # amount = trade_data.amount
                        amount = trade_data.purchased_token_amount
                        amount_to_trade = 0
                        if trade_data.buy_token_if_price is True and trade_data.trade_type == "BUY":
                            # current_price_usd = get_token_symbol_and_price(trade_data.token_address)
                            current_price_usd = current_price
                            buy_condition_met = False
                            # 🔹 2. Check if price meets buy_if_price_up or buy_if_price_down
                            if trade_data.buy_if_price_up and current_price_usd >= trade_data.buy_if_price_up:
                                buy_condition_met = True
                                print(
                                    f"✅ Buying {token_address} as price increased to {current_price_usd} USD (Triggered by buy_if_price_up).")

                            elif trade_data.buy_if_price_down and current_price_usd <= trade_data.buy_if_price_down:
                                buy_condition_met = True
                                print(
                                    f"✅ Buying {token_address} as price dropped to {current_price_usd} USD (Triggered by buy_if_price_down).")
                            if not buy_condition_met:
                                logger.error(
                                    f"[TRADE ERROR] Current price {current_price_usd} USD does not meet buy conditions")
                                continue

                            wallet_balance_response = solana_client.get_balance(Pubkey.from_string(trade_data.to_pubkey))
                            wallet_balance = wallet_balance_response.value / 1_000_000_000  # Convert lamports to SOL
                            sol_amount = trade_data.amount if trade_data.amount else 0

                            # ✅ Check if balance is sufficient
                            if wallet_balance < sol_amount:
                                return jsonify({
                                    "status": "failed",
                                    "message": f"Insufficient balance. Wallet has {wallet_balance:.4f} SOL, but {sol_amount:.4f} SOL is required."
                                }), 400

                            private_key_path = wallet.private_key
                            if not os.path.exists(private_key_path):
                                return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

                            with open(private_key_path, 'rb') as key_file:
                                private_key_bytes = key_file.read()
                                # Create a solders Keypair directly from the private key bytes
                            wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)

                            # Fetch a quote to swap WSOL (Wrapped SOL) to USDC tokens
                            amount_in_lamports = int(sol_amount * 1_000_000_000)

                            quote_params = {
                                "inputMint": "So11111111111111111111111111111111111111112",  # WSOL
                                "outputMint": token_address,  # USDC
                                "amount": amount_in_lamports,  # 0.01 WSOL
                                "slippageBps": int(trade_data.long_slippage)
                            }

                            quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
                            quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)

                            if quote_response.status_code != 200:
                                try:
                                    print(f"Error fetching quote: {quote_response.json()}")
                                except JSONDecodeError as e:
                                    print(f"Error fetching quote: {quote_response.json()}")
                                finally:
                                    continue
                                    # exit()

                            quote_data = quote_response.json()

                            # Fetch the swap transaction for the quote
                            swap_request = {
                                "userPublicKey": str(wallet_keypair.pubkey()),
                                "quoteResponse": quote_data,
                                "computeUnitPriceMicroLamports": int(trade_data.default_gas_fee * 1_000_000),
                                # Convert SOL to micro-lamports
                                "wrapUnwrapSOL": True  # Automatically handle SOL to WSOL conversion
                            }

                            swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
                            swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers)

                            if swap_response.status_code != 200:
                                try:
                                    print(f"Error performing swap: {swap_response.json()}")
                                except JSONDecodeError as e:
                                    print(f"Error performing swap: {swap_response.json()}")
                                finally:
                                    continue
                                    # exit()

                            swap_data = swap_response.json()

                            # Get Raw Transaction
                            swap_transaction_base64 = swap_data["swapTransaction"]
                            swap_transaction_bytes = base64.b64decode(swap_transaction_base64)
                            raw_transaction = VersionedTransaction.from_bytes(swap_transaction_bytes)

                            # Sign Transaction
                            account_keys = raw_transaction.message.account_keys
                            wallet_index = account_keys.index(wallet_keypair.pubkey())

                            signers = list(raw_transaction.signatures)
                            signers[wallet_index] = wallet_keypair

                            signed_transaction = VersionedTransaction(raw_transaction.message, signers)

                            # Send the signed transaction to the RPC client
                            try:
                                start_time = time.time()
                                rpc_response = solana_client.send_transaction(signed_transaction)
                                end_time = time.time()
                                execution_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds
                                signature = str(rpc_response.value)
                                print(f"✅ Auto trade job executed in {execution_time_ms:.2f} ms!")
                                print(f"Transaction sent successfully! Signature: {signature}")
                                print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
                                executed_trade = TradeHistory(
                                    trade_id=trade_data.id if trade_data else None,
                                    token_address=token_address,
                                    trade_type="BUY",
                                    amount=sol_amount,
                                    execution_price=current_price,  # Use the fetched price
                                    tx_id=signature
                                )
                                db.session.add(executed_trade)
                                # db.session.commit()
                                estimated_tokens = int(quote_data.get("outAmount", 0)) / (10 ** quote_data.get("outputMintDecimals", 6))
                                # Calculate estimated tokens received
                                # output_amount = int(quote_data.get("outAmount", 0))
                                # metadata = get_token_metadata(token_address)
                                # if metadata:
                                #     token_decimals = metadata.get("decimals", 0)
                                #     print(f"Token has {token_decimals} decimals")
                                # else:
                                #     token_decimals = 0
                                # # token_decimals = quote_data.get("outputDecimals", 6)  # Get decimals from quote or default to 6
                                # estimated_tokens = output_amount / (10 ** token_decimals)
                                trade_data.purchased_token_amount = estimated_tokens
                                trade_data.buy_token_if_price = False
                                trade_data.trade_type = "SELL"
                                trade_data.initial_price = current_price
                                db.session.commit()
                            except Exception as e:
                                db.session.rollback()  # rollback properly
                                error_message = e.args[0]
                                print("Transaction failed!")
                                print(f"Custom Program Error Code: {error_message.data.err.err.code}")
                                print(f"Message: {error_message.message}")

                        else:
                            message = ""
                            # sell token logic
                            if amount <= 0:
                                continue
                            initial_price = trade_data.initial_price
                            if initial_price <= 0:
                                continue
                            profit_multiplier = current_price / initial_price

                            # Initialize tracking
                            if token_address not in price_tracking:
                                price_tracking[token_address] = {"low": current_price, "sell_time": None,
                                                                 "peak": profit_multiplier}
                            price_tracking[token_address]["low"] = min(price_tracking[token_address]["low"], current_price)
                            price_tracking[token_address]["peak"] = max(price_tracking[token_address]["peak"],
                                                                        profit_multiplier)
                            # SELL CONDITIONS
                            sell_gas_fee = trade_data.long_sell_gas_fee if trade_data.long_sell_gas_fee else 0.01

                            # Track the highest peak after 100% profit
                            if profit_multiplier >= 2.0:  # Ensure peak tracking starts after 100% profit
                                price_tracking[token_address]["peak"] = max(
                                    price_tracking[token_address].get("peak", profit_multiplier),
                                    profit_multiplier
                                )

                            # Auto-Sell 100% if price drops 30% from peak after 100% profit
                            # Auto-Sell 100% if price drops by a user-defined percentage from the peak after 100% profit
                            # elif profit_multiplier <= price_tracking[token_address]["peak"] * 0.70:
                            elif profit_multiplier <= price_tracking[token_address]["peak"] * (
                                    1 - (trade_data.sell_100_after_100_percent_profit_drop / 100)):
                                trade_amount = 1.0  # Sell 100%
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 100% on 30% Profit Drop After 100% Peak"
                                trade_type = "SELL"

                            # Auto-Sell 100% at 30% Drop from Buy Price
                            # elif profit_multiplier < 0.70:
                            elif profit_multiplier < (1 - trade_data.sell_100_at_30_percent_drop / 100):
                                # trade_amount = trade_data.sell_100_at_30_percent_drop / 100
                                trade_amount = 1.0  # SELL 100%
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 100% at 30% Drop"
                                trade_type = "SELL"

                            # Auto-Sell 30% at 200% Profit
                            elif profit_multiplier >= 3.0:
                                trade_amount = trade_data.sell_at_200_percent_profit / 100
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 30% at 200% Profit"
                                trade_type = "SELL"

                            # Auto-Sell 30% at 300% Profit
                            elif profit_multiplier >= 4.0:
                                trade_amount = trade_data.sell_at_300_percent_profit / 100
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 30% at 300% Profit"
                                trade_type = "SELL"

                            # Auto-Sell 10% at 500% Profit
                            elif profit_multiplier >= 6.0:
                                trade_amount = trade_data.sell_at_500_percent_profit / 100
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 10% at 500% Profit"
                                trade_type = "SELL"

                            # Auto-Sell 10% at 1000% Profit
                            elif profit_multiplier >= 11.0:
                                trade_amount = trade_data.sell_at_1000_percent_profit / 100
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 10% at 1000% Profit"
                                trade_type = "SELL"
                            elif profit_multiplier >= 21.0:  # 2000% Profit
                                trade_amount = trade_data.sell_at_2000_percent_profit / 100  # Sell 10%
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 10% at 2000% Profit"
                                trade_type = "SELL"

                            # Auto-Sell 10% at 10,000% Profit
                            elif profit_multiplier >= 101.0:
                                trade_amount = trade_data.sell_at_10000_percent_profit / 100
                                amount_to_trade = amount * trade_amount
                                message = "Auto-Sell 10% at 10,000% Profit"
                                trade_type = "SELL"

                            # EXECUTE TRADE
                            if amount_to_trade > 0:
                                try:
                                    private_key_path = wallet.private_key
                                    if not os.path.exists(private_key_path):
                                        return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

                                    with open(private_key_path, 'rb') as key_file:
                                        private_key_bytes = key_file.read()
                                        # Create a solders Keypair directly from the private key bytes
                                    wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)
                                    token_pubkey = Pubkey.from_string(token_address)
                                    token_info = solana_client.get_token_supply(token_pubkey)
                                    if token_info and hasattr(token_info.value, "decimals"):
                                        decimals = token_info.value.decimals
                                        print(f"Token has {decimals} decimals (fetched from Solana mainnet)")
                                    else:
                                        decimals = 0
                                    # metadata = get_token_metadata(token_address)
                                    # if metadata:
                                    #     decimals = metadata.get("decimals", 0)
                                    #     print(f"Token has {decimals} decimals")
                                    # else:
                                    #     decimals = 0

                                    amount_in_lamports = int(amount_to_trade * (10 ** decimals))

                                    # Fetch a quote to swap WSOL (Wrapped SOL) to USDC tokens
                                    # amount_in_lamports = int(amount * 1_000_000_000)

                                    quote_params = {
                                        "inputMint": token_address,  # WSOL
                                        "outputMint": "So11111111111111111111111111111111111111112",
                                        # amount in raw unit like USDC 6 decimal we sell 5 token then we enter 5*1000000
                                        "amount": amount_in_lamports,
                                        "slippageBps": int(trade_data.long_sell_slippage)
                                    }

                                    quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
                                    quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)

                                    if quote_response.status_code != 200:
                                        try:
                                            print(f"Error fetching quote: {quote_response.json()}")
                                        except JSONDecodeError as e:
                                            print(f"Error fetching quote: {quote_response.json()}")
                                        finally:
                                            continue
                                            # exit()

                                    quote_data = quote_response.json()

                                    print("Quote response:", quote_data)

                                    # Fetch the swap transaction for the quote
                                    swap_request = {
                                        "userPublicKey": str(wallet_keypair.pubkey()),
                                        "quoteResponse": quote_data,
                                        "computeUnitPriceMicroLamports": int(sell_gas_fee * 1_000_000),
                                        # Convert SOL to micro-lamports
                                        "wrapUnwrapSOL": True  # Automatically handle SOL to WSOL conversion
                                    }

                                    swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
                                    swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers)

                                    if swap_response.status_code != 200:
                                        try:
                                            print(f"Error performing swap: {swap_response.json()}")
                                        except JSONDecodeError as e:
                                            print(f"Error performing swap: {swap_response.json()}")
                                        finally:
                                            continue
                                            # exit()

                                    swap_data = swap_response.json()

                                    # Get Raw Transaction
                                    swap_transaction_base64 = swap_data["swapTransaction"]
                                    swap_transaction_bytes = base64.b64decode(swap_transaction_base64)
                                    raw_transaction = VersionedTransaction.from_bytes(swap_transaction_bytes)

                                    # Sign Transaction
                                    account_keys = raw_transaction.message.account_keys
                                    wallet_index = account_keys.index(wallet_keypair.pubkey())

                                    signers = list(raw_transaction.signatures)
                                    signers[wallet_index] = wallet_keypair

                                    signed_transaction = VersionedTransaction(raw_transaction.message, signers)

                                    # Send the signed transaction to the RPC client
                                    try:
                                        start_time = time.time()
                                        rpc_response = solana_client.send_transaction(signed_transaction)
                                        end_time = time.time()
                                        execution_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds
                                        signature = str(rpc_response.value)
                                        logger.info(f"{message}View transaction on Solscan: https://solscan.io/tx/{signature}")
                                        print(f"✅ Auto trade job executed in {execution_time_ms:.2f} ms!")
                                        print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
                                        # Store executed trade in database
                                        executed_trade = TradeHistory(
                                            trade_id=trade_data.id if trade_data else None,
                                            token_address=token_address,
                                            trade_type=trade_type,
                                            amount=amount_to_trade,
                                            execution_price=current_price,  # Use the fetched price
                                            tx_id=signature
                                        )
                                        db.session.add(executed_trade)
                                        db.session.commit()
                                        trade_data.purchased_token_amount -= amount_to_trade
                                        if trade_data.purchased_token_amount <= 0:
                                            # db.session.delete(trade_data)
                                            trade_data.executed = True  # Soft delete instead of actual deletion
                                        db.session.commit()
                                    except Exception as e:
                                        logger.error(f"[TRADE ERROR] {str(e)}")
                                except Exception as e:
                                    logger.error(f"[TRADE ERROR] {str(e)}")
                                    db.session.rollback()  # 🔥 Rollback if an error occurs
                    return json.dumps({'message': 'trade run successfully'})

                except KeyboardInterrupt:
                    logger.info("Auto-trade loop stopped by user.")
                    break
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"Unexpected error in auto_trade loop: {e}")
                    time.sleep(5)  # Prevent crash loops


    # Start background job
    # scheduler = BackgroundScheduler()
    scheduler.add_job(auto_trade, "interval", seconds=5)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
