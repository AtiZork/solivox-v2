import time
import requests
from dotenv import load_dotenv
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from settings import solana_client, Solcan_api_key, moraliz_api_key
from utils import get_token_symbol_and_price, get_token_metadata
from apscheduler.schedulers.background import BackgroundScheduler
import base64
import os
from flask import jsonify, Blueprint
from models import db, Wallet, Trade, TradeLog, AutoSnipeConfig
import logging
import os
import atexit
# Define constants from your parameters
BUY_TXNS_USD = 80
TIME_FROM_LAUNCH = 5  # seconds
BUY_SLIPPAGE_PERCENTAGE = 100
MINIMUM_BUY_TXNS_TIME = 5
BUY_AMOUNT_SOL = 1
PRIORITY_FEE_SOL = 0.01

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


def fetch_autosnipe_config():
    """Fetch user-defined AutoSnipeConfig parameters."""
    config = AutoSnipeConfig.query.first()  # Fetch the first config, or implement multi-config handling
    return {
        "min_transactions": config.min_txns,
        "min_transaction_value": config.buy_txns_over_80_usd,
        "launch_delay": config.launch_delay,
        "buy_amount_sol": config.buy_amount
    }


def auto_buy_token(app):
    scheduler = BackgroundScheduler(daemon=True)

    def auto_buy():
        with app.app_context():
            try:
                # Fetch AutoSnipeConfig
                config = fetch_autosnipe_config()

                # Detect new token (replace with actual detection logic)
                new_tokens = get_new_tokens()  # This should be replaced with real token detection logic

                for token in new_tokens:
                    token_address = token['address']
                    current_price = get_token_symbol_and_price(token_address)['usdPrice']

                    # Fetch transactions for the token
                    transactions = get_token_transactions(token_address, config["min_transactions"], config["min_transaction_value"])

                    # Check if the token meets the transaction conditions
                    total_value = sum(tx['value'] for tx in transactions)
                    if len(transactions) < config["min_transactions"] or total_value < config["min_transaction_value"]:
                        continue  # Skip if the transaction conditions are not met

                    # Check the launch delay condition
                    launch_time = token.get('launch_time',
                                            time.time())  # Use current time if launch_time is unavailable
                    if time.time() - launch_time > config["launch_delay"]:
                        continue  # Skip if launch delay condition is not met

                    # If conditions are met, proceed with buying the token
                    wallet = Wallet.query.filter_by(public_key=token['wallet']).first()
                    if not wallet:
                        logger.warning(f"Wallet not found for {token['wallet']}, skipping buy.")
                        continue

                    private_key_path = wallet.private_key
                    if not os.path.exists(private_key_path):
                        return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

                    with open(private_key_path, 'rb') as key_file:
                        private_key_bytes = key_file.read()
                        wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)

                    amount_in_lamports = int(
                        config["buy_amount_sol"] * 10 ** 9)  # Convert SOL to lamports (1 SOL = 10^9 lamports)

                    # Construct API request for purchasing token
                    quote_params = {
                        "inputMint": token_address,
                        "outputMint": "So11111111111111111111111111111111111111112",  # SOL Mint Address
                        "amount": amount_in_lamports,
                        "slippageBps": int(config["min_transaction_value"] * 100)
                        # Use min transaction value for slippage
                    }

                    quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
                    quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)
                    if quote_response.status_code != 200:
                        logger.error(f"Error fetching quote: {quote_response.json()}")
                        continue

                    quote_data = quote_response.json()
                    logger.info(f"Quote data: {quote_data}")

                    # Perform swap request for token purchase
                    swap_request = {
                        "userPublicKey": str(wallet_keypair.pubkey()),
                        "quoteResponse": quote_data,
                        "computeUnitPriceMicroLamports": int(PRIORITY_FEE_SOL * 1_000_000),
                        "wrapUnwrapSOL": True  # Wrap/Unwrap SOL for the trade
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
                    rpc_response = solana_client.send_transaction(signed_transaction)
                    signature = str(rpc_response.value)
                    logger.info(f"Transaction sent successfully! Signature: {signature}")

                    # Store executed trade in database
                    executed_trade = Trade(
                        trade_type="BUY",
                        token_address=token_address,
                        amount=config["buy_amount_sol"],
                        tx_id=signature
                    )
                    db.session.add(executed_trade)
                    db.session.commit()

                    print(f"Successfully purchased token with tx: {signature}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error in auto_buy loop: {str(e)}")
                time.sleep(1)

    # Start background job
    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_buy, 'interval', seconds=5000)  # Run every 50 seconds
    scheduler.start()

    atexit.register(lambda: scheduler.shutdown())



# def get_new_tokens():
#     """
#     Fetch newly launched tokens from Solscan's API.
#     """
#     solscan_url = "https://api.solscan.io/token/market"  # Solscan API URL for Solana tokens
#     headers = {
#         'x-api-key': Solcan_api_key   # Replace with your actual Solscan API key
#     }
#
#     try:
#         # Fetch new token listings
#         response = requests.get(solscan_url, headers=headers)
#         response.raise_for_status()
#
#         # Example response: Fetch details for new tokens (This can be adjusted based on actual API response)
#         tokens = response.json()
#
#         new_tokens = []
#         for token in tokens:
#             token_data = {
#                 'address': token['address'],  # Mint address of the new token
#                 'name': token['name'],
#                 'symbol': token['symbol'],
#                 'launch_time': token['launch_time']  # Example field: Actual field z on API response
#             }
#             new_tokens.append(token_data)
#
#         return new_tokens
#
#     except requests.exceptions.RequestException as e:
#         print(f"Error fetching new tokens from Solscan: {e}")
#         return []

MORALIS_API_KEY = moraliz_api_key
MORALIS_SERVER_URL = 'https://solana-mainnet.moralis.io:2053/server'

def get_new_tokens():
    """
    Fetch newly launched tokens from the Solana network using Moralis API.
    """
    url = f"{MORALIS_SERVER_URL}/solana/mainnet/transactions"
    headers = {
        'x-api-key': MORALIS_API_KEY
    }

    # Example parameters for Solana token mint transactions (you may need to adjust based on your requirements)
    params = {
        'chain': 'solana',
        'limit': 10,
        'order': 'desc'
    }

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()

        transactions = response.json()

        new_tokens = []
        for txn in transactions:
            # Here, you can filter transactions that are related to token mints (minting action)
            if txn.get('type') == 'mint':
                new_tokens.append({
                    'address': txn.get('tokenAddress', 'Unknown'),
                    'tx_signature': txn['transactionHash'],
                    'amount': txn.get('amount', 0)
                })

        return new_tokens

    except requests.exceptions.RequestException as e:
        print(f"Error fetching new tokens from Moralis: {e}")
        return []



def get_transaction_details(signature):
    """Fetch full details of a transaction based on the signature"""
    solana_rpc_url = "https://api.mainnet-beta.solana.com"  # Solana RPC URL for mainnet
    params = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getConfirmedTransaction",
        "params": [signature, {"encoding": "jsonParsed"}]
    }

    try:
        response = requests.post(solana_rpc_url, json=params)
        response.raise_for_status()
        return response.json().get("result", {})

    except requests.exceptions.RequestException as e:
        print(f"Error fetching transaction details: {e}")
        return {}


# Helper function to fetch transactions of a token (example logic)
def get_token_transactions(token_address, min_txns, min_value_usd):
    """
    Fetch transactions for the given token address. We assume the token is on Solana and use its RPC API.
    Returns a list of transactions, each with a 'value' field indicating USD value of the transaction.

    token_address: str - The address of the token (mint address).
    min_txns: int - Minimum number of transactions to fetch.
    min_value_usd: float - Minimum USD value of the transactions to consider.
    """
    solana_rpc_url = "https://api.mainnet-beta.solana.com"  # Solana mainnet RPC URL

    # Request to get recent confirmed transactions for the token address
    params = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getConfirmedSignaturesForAddress2",
        "params": [token_address, {"limit": 100}]  # Fetch last 100 transactions
    }

    try:
        response = requests.post(solana_rpc_url, json=params)
        response.raise_for_status()

        transactions = response.json().get('result', [])
        if not transactions:
            return []  # No transactions found

        transaction_details = []
        total_value = 0

        # Process each transaction
        for tx in transactions:
            # Here we assume the tx['meta']['postTokenBalances'] contains the transaction value in lamports
            # This is a basic assumption, you will need to adjust it based on actual data
            for token_balance in tx.get('meta', {}).get('postTokenBalances', []):
                if token_balance['mint'] == token_address:
                    # Convert the value from lamports to SOL and then fetch USD price
                    lamports = token_balance['uiAmount']
                    value_in_usd = lamports * get_token_symbol_and_price(token_address)['usdPrice']

                    if value_in_usd >= min_value_usd:
                        transaction_details.append({'value': value_in_usd, 'signature': tx['signature']})
                        total_value += value_in_usd

        # Check if there are enough transactions
        if len(transaction_details) >= min_txns:
            return transaction_details
        else:
            return []  # Not enough valid transactions

    except requests.exceptions.RequestException as e:
        print(f"Error fetching transactions: {e}")
        return []
