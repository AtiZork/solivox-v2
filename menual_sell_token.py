import os
import time
import logging
from models import db, Wallet, Trade, TradeLog, TradeHistory
import requests
import base64
from flask import jsonify, Blueprint, request
from requests import JSONDecodeError
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from settings import solana_client
from dotenv import load_dotenv
from utils import get_token_symbol_and_price, get_token_metadata
from solders.pubkey import Pubkey

load_dotenv()
API_KEY = os.getenv("API_KEY")
# Free tier users should use lite-api.jup.ag. api.jup.ag is for paid plans and requires an API key
API_BASE_URL = "https://api.jup.ag" if API_KEY else "https://lite-api.jup.ag"
# Set up headers for API requests (include x-api-key if API_KEY is available)
headers = {"x-api-key": API_KEY} if API_KEY else {}
manual_sell_trade_bp = Blueprint('manual_sell_trade_bp', __name__)

price_tracking = {}


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


def get_token_balance(wallet_address, token_mint):
    try:
        owner = (Pubkey.from_string(wallet_address))
        mint = (Pubkey.from_string(token_mint))

        response = solana_client.get_token_accounts_by_owner(
            owner,
            {"mint": str(mint)},
            commitment="confirmed"
        )

        accounts = response.get("result", {}).get("value", [])
        total_balance = 0.0

        for acc in accounts:
            parsed_data = acc["account"]["data"]
            if isinstance(parsed_data, dict) and "parsed" in parsed_data:
                token_amount = parsed_data["parsed"]["info"]["tokenAmount"]
                total_balance += float(token_amount["uiAmount"])

        return round(total_balance, 6)

    except Exception as e:
        print(f"Error: {e}")
        return 0.0


@manual_sell_trade_bp.route('/sell_token/<int:trade_id>/', methods=['PUT'])
def sell_token(trade_id):
    try:
        last_trade = Trade.query.filter_by(id=trade_id).first()
        token_address = last_trade.token_address
        to_pubkey = last_trade.to_pubkey
        # balance = get_token_balance(to_pubkey, token_address)

        # # Calculate amount to sell
        data = request.json
        sell_percent = data.get('sell_percent')  # sell % like 10%, 25%, etc.
        sell_amount = data.get('sell_amount')  # Optional: sell exact amount
        total_tokens = last_trade.purchased_token_amount

        # Fetch token and SOL prices in USD
        current_token_price_ = get_token_symbol_and_price(token_address)
        token_price_usd = current_token_price_["usdPrice"]
        sol_price_usd = get_token_symbol_and_price("So11111111111111111111111111111111111111112")["usdPrice"]

        if not token_price_usd or not sol_price_usd:
            return jsonify({"status": "failed", "message": "Failed to fetch token or SOL price"}), 400

        # Calculate token amount to sell
        if sell_amount and sell_amount > 0:
            usd_to_sell = sell_amount * sol_price_usd
            amount = usd_to_sell / token_price_usd
        elif sell_percent and sell_percent > 0:
            amount = (sell_percent / 100.0) * total_tokens
        else:
            return jsonify({"status": "failed", "message": "Provide either sell_amount_sol or sell_percent"}), 400

        # if sell_amount > 0:
        #     amount = sell_amount
        # else:
        #     amount = (sell_percent / 100.0) * total_tokens
        if amount <= 0:
            return jsonify({"status": "failed", "message": "Sell token amount must be greater then 0"}), 400
        if amount > total_tokens:
            return jsonify({"status": "failed", "message": "Sell token amount greater then available tokens"}), 400
        current_token_price_ = get_token_symbol_and_price(token_address)
        current_token_price = current_token_price_["usdPrice"]
        wallet = Wallet.query.filter_by(public_key=to_pubkey).first()
        private_key_path = wallet.private_key
        if not os.path.exists(private_key_path):
            return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

        with open(private_key_path, 'rb') as key_file:
            private_key_bytes = key_file.read()
            # Create a solders Keypair directly from the private key bytes
        wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)
        metadata = get_token_metadata(token_address)
        if metadata:
            decimals = metadata.get("decimals", 0)
            print(f"Token has {decimals} decimals")
        else:
            decimals = 0

        amount_in_lamports = int(amount * (10 ** decimals))

        quote_params = {
            "inputMint": token_address,  # WSOL
            "outputMint": "So11111111111111111111111111111111111111112",
            # amount in raw unit like USDC 6 decimal we sell 5 token then we enter 5*1000000
            "amount": amount_in_lamports,
            "slippageBps": int(last_trade.long_sell_slippage)
        }

        quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
        quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)

        if quote_response.status_code != 200:
            try:
                print(f"Error fetching quote: {quote_response.json()}")
                return jsonify({"status": "failed", "error": "Error fetching quote"}), 400
            except JSONDecodeError as e:
                print(f"Error fetching quote: {quote_response.json()}")
                return jsonify({"status": "failed", "error": "Error fetching quote"}), 400

        quote_data = quote_response.json()

        # Fetch the swap transaction for the quote
        swap_request = {
            "userPublicKey": str(wallet_keypair.pubkey()),
            "quoteResponse": quote_data,
            "computeUnitPriceMicroLamports": int(last_trade.long_sell_gas_fee * 1_000_000),
            # Convert SOL to micro-lamports
            "wrapUnwrapSOL": True  # Automatically handle SOL to WSOL conversion
        }

        swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
        swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers)

        if swap_response.status_code != 200:
            try:
                print(f"Error performing swap: {swap_response.json()}")
                return jsonify({"status": "failed", "error": "Error performing swap"}), 400
            except JSONDecodeError as e:
                print(f"Error performing swap: {swap_response.json()}")
                return jsonify({"status": "failed", "error": "Error performing swap"}), 400
            # finally:
            #     exit()

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
            logger.info(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
            print(f"✅ Auto trade job executed in {execution_time_ms:.2f} ms!")
            print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
            # Store executed trade in database
            executed_trade = TradeHistory(
                trade_id=last_trade.id if last_trade else None,
                token_address=token_address,
                trade_type="Manual SEll",
                amount=amount,
                execution_price=current_token_price,  # Use the fetched price
                tx_id=signature
            )
            db.session.add(executed_trade)
            db.session.commit()
            last_trade.purchased_token_amount -= amount
            if last_trade.purchased_token_amount <= 0:
                # db.session.delete(trade_data)
                last_trade.executed = True  # Soft delete instead of actual deletion
            db.session.commit()
            return jsonify({"status": "success", "message": "Token SELL Successfully"}), 200
        except Exception as e:
            logger.error(f"[TRADE ERROR] {str(e)}")
            return jsonify({"status": "failed", "error": str(e)}), 500
    except Exception as e:
        logger.error(f"[TRADE ERROR] {str(e)}")
        db.session.rollback()  # 🔥 Rollback if an error occurs
        return jsonify({"status": "failed", "error": str(e)}), 500
