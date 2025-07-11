import os
import time
import logging
from models import db, Wallet, Trade, TradeHistory, AutoSnipeConfig, TradeLog
import requests
import base64
from flask import jsonify, Blueprint
from dotenv import load_dotenv
from requests import JSONDecodeError
from solana.rpc.core import RPCException
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from settings import solana_client
from utils import get_token_symbol_and_price
from solders.pubkey import Pubkey

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
# SPL Token Program ID
SPL_TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")


def get_token_price_usd(token_address):
    try:
        current_token_price_ = get_token_symbol_and_price(token_address)
        current_token_price = current_token_price_["usdPrice"]
        return current_token_price
    except Exception as e:
        return 0


def get_token_transactions(token_address: str, limit: int) -> list[dict]:
    """
    Fetches recent transactions for a given token address.
    Parses them to extract a heuristic 'amount' for demonstration purposes.
    """
    txs_data = []
    try:
        token_address = Pubkey.from_string(token_address)
        signatures_response = solana_client.get_signatures_for_address(token_address, limit=limit)
        signatures = signatures_response.value

        for siginfo in signatures:
            sig = siginfo.signature
            try:
                tx_response = solana_client.get_transaction(sig, encoding='jsonParsed',
                                                            max_supported_transaction_version=0)
                tx_data = tx_response.value

                if not tx_data:
                    continue

                tx_json = tx_data.to_json()
                if 'transaction' not in tx_json or 'meta' not in tx_json:
                    continue

                amount = 0.0
                token_post_balances = tx_data.transaction.meta.post_token_balances
                token_pre_balances = tx_data.transaction.meta.pre_token_balances

                for post_bal in token_post_balances:
                    if post_bal.mint == token_address:
                        pre_bal = next((
                            pb for pb in token_pre_balances
                            if pb.owner == post_bal.owner and pb.mint == token_address
                        ), None)

                        if pre_bal:
                            post_ui_amount = post_bal.ui_token_amount.ui_amount
                            pre_ui_amount = pre_bal.ui_token_amount.ui_amount
                            if post_ui_amount is not None and pre_ui_amount is not None:
                                balance_change = abs(post_ui_amount - pre_ui_amount)
                                if balance_change > amount:
                                    amount = balance_change

                if amount == 0.0:
                    amount = 1.0

                txs_data.append({
                    'amount': amount,
                    'signature': str(sig),
                    'token_address': token_address
                })

            except Exception as e:
                print(f"    [Tx Fetcher] Error parsing transaction {sig}: {e}")
                continue
        return txs_data
    except Exception as e:
        print(f"[Tx Fetcher] Error fetching signatures for {token_address}: {e}")
        return []

def get_token_specific_transactions(token_address, min_required, min_usd_value):
    """
    Fetches recent transactions for a given token mint address.
    Returns only those where the USD value meets or exceeds the threshold.
    Continues fetching until min_required qualifying transactions are collected.
    """
    txs_data = []
    seen_signatures = set()
    before = None  # for pagination

    try:
        token_address_pubkey = Pubkey.from_string(token_address)

        while len(txs_data) < min_required:
            signatures_response = solana_client.get_signatures_for_address(
                token_address_pubkey, limit=100, before=before
            )
            signatures = signatures_response.value
            if not signatures:
                break

            for siginfo in signatures:
                sig = siginfo.signature
                before = sig  # update for next pagination
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)

                try:
                    tx_response = solana_client.get_transaction(sig, encoding='jsonParsed',
                                                                max_supported_transaction_version=0)
                    tx_data = tx_response.value
                    if not tx_data:
                        continue

                    tx_json = tx_data.to_json()
                    if 'transaction' not in tx_json or 'meta' not in tx_json:
                        continue

                    amount = 0.0
                    token_post_balances = tx_data.transaction.meta.post_token_balances
                    token_pre_balances = tx_data.transaction.meta.pre_token_balances

                    for post_bal in token_post_balances:
                        if post_bal.mint == token_address_pubkey:
                            pre_bal = next((
                                pb for pb in token_pre_balances
                                if pb.owner == post_bal.owner and pb.mint == token_address_pubkey
                            ), None)

                            if pre_bal:
                                post_ui_amount = post_bal.ui_token_amount.ui_amount
                                pre_ui_amount = pre_bal.ui_token_amount.ui_amount
                                if post_ui_amount is not None and pre_ui_amount is not None:
                                    balance_change = abs(post_ui_amount - pre_ui_amount)
                                    if balance_change > amount:
                                        amount = balance_change

                    if amount == 0.0:
                        continue
                        # amount = 1.0  # default fallback

                    usd_value = price_usd_func({'amount': amount, 'token_address': token_address})
                    if usd_value >= min_usd_value:
                        txs_data.append({
                            'amount': amount,
                            'usd_value': usd_value,
                            'signature': str(sig),
                            'token_address': token_address
                        })

                    if len(txs_data) >= min_required:
                        break

                except Exception as e:
                    print(f"    [Tx Fetcher] Error parsing transaction {sig}: {e}")
                    continue

        return txs_data

    except Exception as e:
        print(f"[Tx Fetcher] Error fetching signatures for {token_address}: {e}")
        return []

def convert_sol_to_usd(sol_amount):
    token_address = 'So11111111111111111111111111111111111111112'
    current_sol_price = get_token_symbol_and_price(token_address)  # Replace with API fetch if needed
    return sol_amount * current_sol_price


def price_usd_func(txn: dict) -> float:
    """
    Calculates the USD value of a given transaction's amount.
    """
    amount = txn.get('amount', 0)
    token_address = txn.get('token_address', None)
    if not token_address:
        print(
            f"  [Price Func] Warning: token_address missing in transaction data for signature {txn.get('signature')}.")
        return 0.0

    token_usd_price = get_token_price_usd(token_address)
    return amount * token_usd_price


def should_buy_token(token_address: str, user_id: int) -> bool:
    """
    Checks if a token meets the buy conditions from AutoSnipeConfig for a user.
    """
    config = AutoSnipeConfig.query.filter_by(user_id=user_id, active=True).first()
    if not config:
        print(f"  [Buy Condition] No active AutoSnipeConfig found for user {user_id}. Cannot determine buy conditions.")
        return False

    start_time = time.time()
    check_duration = config.launch_delay  # seconds
    print(
        f"  [Buy Condition] Checking buy conditions for {token_address} for user {user_id} for max {check_duration} seconds...")

    while time.time() - start_time < check_duration:
        # token_address = 'So11111111111111111111111111111111111111112'
        # txns = get_token_transactions(token_address, config.min_txns)  # Fetch more than min_txns to ensure we have enough to check
        txns = get_token_specific_transactions(token_address, config.min_txns, config.buy_txns_over_80_usd)  # Fetch more than min_txns to ensure we have enough to check
        if not txns:
            time.sleep(1)
            continue

        qualifying_txns_count = 0
        for txn in txns:
            usd_value = txn.usd_value if txn.usd_value else 0.0
            if usd_value >= config.buy_txns_over_80_usd:
                qualifying_txns_count += 1
        print(f"[Buy Condition] Current qualifying transactions: {qualifying_txns_count} / required: {config.min_txns}.")

        if qualifying_txns_count >= config.min_txns:
            print(f"[Buy Condition] Buy conditions MET for {token_address}: {qualifying_txns_count} transactions >= ${config.buy_txns_over_80_usd}.")
            return True
        time.sleep(1)

    print(f"[Buy Condition] Buy conditions NOT MET for {token_address} within {check_duration} seconds.")
    return False


def buy_token(token_address: str, user_id: int) -> bool:
    global signature
    try:
        config = AutoSnipeConfig.query.filter_by(user_id=user_id, active=True).first()
        if not config:
            logger.error(f"[Buy Token] No active AutoSnipeConfig found for user. Cannot proceed with buy.")
            return

        # Fetch relevant settings from the AutoSnipeConfig
        # These are used for the actual swap execution parameters
        buy_amount_sol = config.buy_amount if config.buy_amount else 1.0
        buy_slippage_percent_bps = int((config.slippage if config.slippage else 100) * 100) # Convert percentage to BPS
        priority_fee_sol = config.priority_fee if config.priority_fee else 0.01

        wallet = Wallet.query.filter_by(user_id=user_id).first() # Assuming user_id can link to a wallet
        if not wallet:
            logger.error(f"Failed to fetch wallet for user.")
            return
        to_pubkey = wallet.public_key

        amount = buy_amount_sol # Amount in SOL to spend
        current_token_price_data = get_token_symbol_and_price(token_address)
        current_token_price = current_token_price_data["usdPrice"]
        token_name = current_token_price_data["name"]
        token_symbol = current_token_price_data["symbol"]
        estimated_tokens = 0 # Will be updated after quote


        # ✅ Check if wallet exists and balance is sufficient
        wallet_balance_response = solana_client.get_balance(Pubkey.from_string(to_pubkey))
        wallet_balance = wallet_balance_response.value / 1_000_000_000  # Convert lamports to SOL

        if wallet_balance < amount:
            logger.error(f"Insufficient balance. Wallet has {wallet_balance:.4f} SOL, but {amount:.4f} SOL is required.")
            return

        private_key_path = wallet.private_key
        if not os.path.exists(private_key_path):
            logger.error(f"Failed to fetch private key")
            return

        with open(private_key_path, 'rb') as key_file:
            private_key_bytes = key_file.read()
        wallet_keypair = SoldersKeypair.from_seed(private_key_bytes[:32]) # Ensure only 32 bytes for seed

        # Fetch a quote to swap WSOL (Wrapped SOL) to target token
        amount_in_lamports = int(amount * 1_000_000_000)

        quote_params = {
            "inputMint": "So11111111111111111111111111111111111111112",  # WSOL
            "outputMint": token_address,
            "amount": amount_in_lamports,
            "slippageBps": buy_slippage_percent_bps
        }

        quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
        quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)

        if quote_response.status_code != 200:
            try:
                print(f"Error fetching quote: {quote_response.json()}")
                logger.error(f"Error fetching quote")
                return
            except JSONDecodeError:
                print(f"Error fetching quote: {quote_response.text}")
                logger.error(f"Error fetching quote (non-JSON response)")
                return

        quote_data = quote_response.json()
        estimated_tokens = int(quote_data.get("outAmount", 0)) / (10 ** quote_data.get("outputMintDecimals", 6))

        # Fetch the swap transaction for the quote
        swap_request = {
            "userPublicKey": str(wallet_keypair.pubkey()),
            "quoteResponse": quote_data,
            "computeUnitPriceMicroLamports": int(priority_fee_sol * 1_000_000),  # Convert SOL to micro-lamports
            "wrapUnwrapSOL": True
        }

        swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
        swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers)

        if swap_response.status_code != 200:
            try:
                print(f"Error performing swap: {swap_response.json()}")
                logger.error(f"Error performing swap)")
                return
            except JSONDecodeError:
                print(f"Error performing swap: {swap_response.text}")
                logger.error(f"Error performing swap (non-JSON response)")
                return

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

        # # Sign Transaction
        # signed_transaction = VersionedTransaction(raw_transaction.message, [wallet_keypair.sign_message(raw_transaction.message.serialize())])

        # Send the signed transaction to the RPC client
        try:
            start_time = time.time()
            rpc_response = solana_client.send_transaction(signed_transaction)
            end_time = time.time()
            execution_time_ms = (end_time - start_time) * 1000
            signature = str(rpc_response.value)
            print(f"✅ Auto trade job executed in {execution_time_ms:.2f} ms!")
            print(f"Transaction sent successfully! Signature: {signature}")
            print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")

        except RPCException as e:
            error_message = e.args[0]
            print("Transaction failed!")
            print(f"Custom Program Error Code: {error_message.data.err.err.code}")
            print(f"Message: {error_message.message}")
            logger.error(error_message.message)
            return

        # Create new Trade record with simplified fields
        new_trade = Trade(
            user_id=int(user_id),
            config_id=config.id,
            token_address=token_address,
            token_name=token_name,
            token_symbol=token_symbol,
            initial_price=current_token_price,
            amount=amount, # Amount of SOL spent
            purchased_token_amount=estimated_tokens,
            trade_type="BUY",
            to_pubkey=to_pubkey,

            # Autosnipe settings from the config
            auto_snipe=True, # This trade was initiated by autosnipe logic
            trade_kind="AUTOSNIPE", # Explicitly marking it as an AUTOSNIPE trade
            autosnipe_sell_slippage=config.slippage if config.slippage else 0.30, # Use config slippage for sell if no dedicated field
            drop_cutoff=config.drop_cutoff if config.drop_cutoff else 30,
            drop_until_profit=config.drop_until_profit if config.drop_until_profit else 99,
            drop_after_100=config.drop_after_100 if config.drop_after_100 else 50,
            drop_after_400=config.drop_after_400 if config.drop_after_400 else 30,
            sell_at_200=config.sell_at_200 if config.sell_at_200 else 10,
            sell_at_400=config.sell_at_400 if config.sell_at_400 else 10,
            sell_at_1000=config.sell_at_1000 if config.sell_at_1000 else 10,
            sell_at_1500=config.sell_at_1500 if config.sell_at_1500 else 10,
            sell_at_2500=config.sell_at_2500 if config.sell_at_2500 else 10,
            sell_at_4000=config.sell_at_4000 if config.sell_at_4000 else 10,
            sell_at_10000=config.sell_at_10000 if config.sell_at_10000 else 10,
        )
        db.session.add(new_trade)
        db.session.commit()

        # Record in TradeHistory
        executed_trade = TradeHistory(
            trade_id=new_trade.id,
            token_address=token_address,
            trade_type="BUY",
            trade_kind="AUTOSNIPE", # Consistent with Trade table
            amount=amount, # Amount of SOL spent
            execution_price=current_token_price,
            tx_id=signature
        )
        db.session.add(executed_trade)
        db.session.commit()
        logger.info(f"AutoSnipe Trade against token address {token_address} run successfully")
        return True

    except Exception as e:
        db.session.rollback()
        print(f"An unexpected error occurred during buy_token: {str(e)}")
        # Log full traceback for debugging
        import traceback
        traceback.print_exc()
        logger.error(f"An error occurred: {str(e)}")
        return

def autosnipe_buy_new_token(token_address: str, user_id: int) -> bool:
    """
    Orchestrates the check and buy process for a newly detected token.
    """
    print(f"\n--- Initiating autosnipe check for token: {token_address} (User: {user_id}) ---")
    if should_buy_token(token_address, user_id):
        print(f"Token {token_address} met buy conditions. Attempting to buy...")
        return buy_token(token_address, user_id)
    else:
        print(f"Token {token_address} did not meet buy conditions. Skipping buy.")
        return False

# Initialize variables
last_checked_slot = 0  # Start from slot 0
processed_token_mints = set()  # Create an empty set to track processed token mints

def detect_new_tokens_single_pass(last_checked_slot: int, processed_token_mints: set) -> int:
    """
    Performs a single pass of detecting new SPL token mints and triggering autosnipe.
    Returns the current slot after checking.
    """
    try:
        config = AutoSnipeConfig.query.filter_by(active=True).first()
        user_id = int(config.user_id)
        current_slot_response = solana_client.get_slot()
        current_slot = current_slot_response.value

        if current_slot <= last_checked_slot:
            return last_checked_slot

        print(f"[Detector] Checking new blocks from slot {last_checked_slot + 1} to {current_slot}...")

        signatures_response = solana_client.get_signatures_for_address(
            SPL_TOKEN_PROGRAM_ID,
            limit=50
        )
        signatures = signatures_response.value

        new_mints_found_in_batch = set()

        for siginfo in signatures:
            signature = siginfo.signature
            if siginfo.slot > last_checked_slot:
                try:
                    # time.sleep(4)  # Add sleep to avoid rate limiting
                    tx_response = solana_client.get_transaction(signature, encoding='jsonParsed',
                                                                max_supported_transaction_version=0)
                    tx_data = tx_response.value

                    if not tx_data:
                        continue

                    instructions = tx_data.transaction.transaction.message.instructions
                    for instruction in instructions:
                        # if (instruction.get('programId') == str(SPL_TOKEN_PROGRAM_ID) and
                        #         'parsed' in instruction and
                        #         instruction['parsed'].get('type') == 'initializeMint'):
                        if instruction.program_id == SPL_TOKEN_PROGRAM_ID:
                            print(instruction.parsed['type'])
                            if instruction.parsed.get('type') == 'initializeMint':
                                if (instruction.program_id == SPL_TOKEN_PROGRAM_ID and
                                        hasattr(instruction, 'parsed') and
                                        instruction.parsed.get('type') == 'initializeMint'):
                                    # mint_address = instruction['parsed']['info'].get('mint')
                                    mint_address = instruction.parsed['info'].get('mint')
                                    if mint_address and mint_address not in processed_token_mints:
                                        print(f"[Detector] Found NEW TOKEN MINT: {mint_address} (Signature: {signature})")
                                        new_mints_found_in_batch.add(mint_address)
                                        processed_token_mints.add(mint_address)

                except Exception as e:
                    print(f"[Detector] Error processing transaction {signature}: {e}")
                    continue
        token_address = 'D2QvT2fgdvaLxDLiTFjHeRqeZFXU8UqFdJr7xcgHmoon'
        # txns = get_token_transactions(token_address, config.min_txns)
        txns = get_token_specific_transactions(token_address, config.min_txns, config.buy_txns_over_80_usd)
        print(txns)
        for new_token_address in new_mints_found_in_batch:
            print(f"[Detector] Attempting autosnipe for detected new token: {new_token_address}")
            autosnipe_buy_new_token(new_token_address, user_id)
        return current_slot
    except Exception as e:
        print(f"[Detector] An error occurred in detection pass: {e}")
        return last_checked_slot

autosnipe_trade_bp = Blueprint('autosnipe_trade_bp', __name__)


@autosnipe_trade_bp.route('/detect_new_tokens', methods=['POST'])
def detect_new_tokens():
    global last_checked_slot, processed_token_mints
    try:
        # Call the function
        current_slot = detect_new_tokens_single_pass(last_checked_slot, processed_token_mints)

        # Update last_checked_slot
        last_checked_slot = current_slot

        return jsonify({
            "status": "success",
            "last_checked_slot": last_checked_slot,
            "processed_token_mints": list(processed_token_mints)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# from apscheduler.schedulers.background import BackgroundScheduler
# import atexit
#
# # Initialize variables
# last_checked_slot = 0  # Start from slot 0
# processed_token_mints = set()  # Create an empty set to track processed token mints
#
# def run_detector():
#     global last_checked_slot, processed_token_mints
#     last_checked_slot = detect_new_tokens_single_pass(last_checked_slot, processed_token_mints)
#
# # Set up the scheduler
# scheduler = BackgroundScheduler()
# scheduler.add_job(run_detector, 'interval', seconds=10)  # Run every 10 seconds
# scheduler.start()
#
# # Ensure the scheduler shuts down gracefully on exit
# atexit.register(lambda: scheduler.shutdown())
#
# print("Detector is running in the background...")