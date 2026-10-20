import os
import time
import logging
from models import db, Wallet, Trade, TradeLog, TradeHistory
import requests
import base64
from flask import jsonify, Blueprint, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from requests import JSONDecodeError
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from settings import solana_client
from dotenv import load_dotenv
from shyft_pricing import get_token_price
from utils import get_token_metadata
from solders.pubkey import Pubkey
# Reused from the auto-sell bot so manual sells get the same safety
# properties instead of a second, independently-maintained implementation:
# a per-trade lock (so this can't race the auto-sell bot into a double
# sell) and a real on-chain balance read (instead of trusting the DB's
# purchased_token_amount, which can drift from what the wallet actually
# holds).
from autosnipe_sell_script import _trade_lock, get_wallet_token_balance

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
@jwt_required()
def sell_token(trade_id):
    user_id = get_jwt_identity()
    last_trade = Trade.query.filter_by(id=trade_id, user_id=user_id).first()
    if not last_trade:
        return jsonify({"status": "failed", "message": "Trade not found"}), 404

    # Without this, a manual sell could race the auto-sell bot's own
    # cycle/instant path (or another manual-sell click) into sending two
    # sells for the same position at once.
    lock = _trade_lock(trade_id)
    if not lock.acquire(blocking=False):
        return jsonify({
            "status": "failed",
            "message": "This trade is already being sold (by the auto-sell bot or another request) - try again shortly.",
        }), 409
    try:
        db.session.refresh(last_trade)
        if last_trade.executed:
            return jsonify({"status": "failed", "message": "This position is already fully sold"}), 400

        token_address = last_trade.token_address
        to_pubkey = last_trade.to_pubkey

        data = request.json
        sell_percent = data.get('sell_percent')  # sell % like 10%, 25%, etc.
        sell_amount = data.get('sell_amount')  # Optional: sell exact amount (in SOL)
        total_tokens = last_trade.purchased_token_amount
        token_price_usd = None
        sol_price_usd = None

        # Fetch token and SOL prices in USD
        current_token_price_ = get_token_price(token_address)
        if current_token_price_['usdPrice'] is not None and current_token_price_["sol_price_usd"]:
            token_price_usd = current_token_price_["usdPrice"]  # price in used
            sol_price_usd = current_token_price_["sol_price_usd"]
            if not sol_price_usd:
                sol_price_usd = get_token_price("So11111111111111111111111111111111111111112")["usdPrice"]

        if not token_price_usd or not sol_price_usd: # price in sol
            return jsonify({"status": "failed", "message": "Failed to fetch token or SOL price"}), 400

        # Calculate token amount to sell
        if sell_amount and sell_amount > 0:
            usd_to_sell = sell_amount * sol_price_usd
            amount = usd_to_sell / token_price_usd
        elif sell_percent and sell_percent > 0:
            amount = (sell_percent / 100.0) * total_tokens
        else:
            return jsonify({"status": "failed", "message": "Provide either sell_amount_sol or sell_percent"}), 400

        if amount <= 0:
            return jsonify({"status": "failed", "message": "Sell token amount must be greater then 0"}), 400
        if amount > total_tokens:
            return jsonify({"status": "failed", "message": "Sell token amount greater then available tokens"}), 400

        # Cap to what the wallet actually holds on-chain - purchased_token_amount
        # is the DB's bookkeeping estimate and can drift from reality (a prior
        # partial sell that landed for slightly less than requested, a buy
        # quote that over/under-estimated the fill, etc.).
        try:
            raw_balance, decimals = get_wallet_token_balance(Pubkey.from_string(to_pubkey), token_address)
            real_balance = raw_balance / (10 ** decimals) if decimals else 0.0
        except Exception as e:
            return jsonify({"status": "failed", "message": f"Could not verify on-chain balance: {e}"}), 400
        if real_balance <= 0:
            return jsonify({"status": "failed", "message": "Wallet holds 0 of this token on-chain"}), 400
        amount = min(amount, real_balance)

        current_token_price_ = get_token_price(token_address)
        current_token_price = current_token_price_["usdPrice"]
        wallet = Wallet.query.filter_by(public_key=to_pubkey, user_id=user_id).first()
        private_key_path = wallet.private_key
        if not os.path.exists(private_key_path):
            return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

        with open(private_key_path, 'rb') as key_file:
            private_key_bytes = key_file.read()
        # Wallets created via attach/recover can store either a 32-byte seed
        # or a 64-byte raw keypair (build_keypair_from_private_key accepts
        # both) - from_seed() alone raises for the 64-byte case, matching the
        # auto-sell bot's own handling of both lengths.
        if len(private_key_bytes) == 64:
            wallet_keypair = SoldersKeypair.from_bytes(private_key_bytes)
        else:
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

        amount_in_lamports = int(amount * (10 ** decimals))

        quote_params = {
            "inputMint": token_address,  # WSOL
            "outputMint": "So11111111111111111111111111111111111111112",
            # amount in raw unit like USDC 6 decimal we sell 5 token then we enter 5*1000000
            "amount": amount_in_lamports,
            "slippageBps": int((last_trade.long_sell_slippage or 20) * 100),  # percent -> bps
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
            # Total lamports to spend on priority, not a per-CU price (see
            # autosnipe_buy_new_token.py's swap_request for the full note).
            "prioritizationFeeLamports": int(last_trade.long_sell_gas_fee * 1_000_000_000),
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
            # rpc_response = False
            end_time = time.time()
            execution_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds
            signature = str(rpc_response.value)
            logger.info(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
            print(f"✅ Auto trade job executed in {execution_time_ms:.2f} ms!")
            print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
            # trade_type="SELL" + status="sent": a send succeeding only means
            # the RPC node accepted it for broadcast, not that it landed -
            # this hooks the manual sell into the auto-sell bot's own
            # reconcile_pending_sells() job, which confirms it shortly after
            # (or restores these tokens to the open position if the send
            # never actually landed on-chain). Previously this recorded no
            # "sent" row at all, so a failed manual sell permanently reduced
            # the position with nothing to ever recover it.
            executed_trade = TradeHistory(
                trade_id=last_trade.id if last_trade else None,
                token_address=token_address,
                trade_type="SELL",
                amount=amount,
                execution_price=current_token_price,  # Use the fetched price
                tx_id=signature,
                status="sent",
            )
            db.session.add(executed_trade)
            db.session.commit()
            last_trade.purchased_token_amount -= amount
            if last_trade.purchased_token_amount <= 0:
                last_trade.executed = True
            db.session.commit()
            return jsonify({"status": "success", "message": "Token SELL Successfully"}), 200
        except Exception as e:
            logger.error(f"[TRADE ERROR] {str(e)}")
            return jsonify({"status": "failed", "error": str(e)}), 500
    except Exception as e:
        logger.error(f"[TRADE ERROR] {str(e)}")
        db.session.rollback()  # 🔥 Rollback if an error occurs
        return jsonify({"status": "failed", "error": str(e)}), 500
    finally:
        lock.release()
