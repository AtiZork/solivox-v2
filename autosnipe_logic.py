import asyncio
import atexit
import json
import os
import base64
import threading
import time
import logging
import requests
from dotenv import load_dotenv
# Solana SDK imports
from solders.pubkey import Pubkey
from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import VersionedTransaction
from websockets import connect # <-- THIS IS THE CORRECT IMPORT FOR WEBSOCKETS
from solana.rpc.commitment import Processed, Confirmed

# Local imports from your project
# Ensure these paths are correct relative to autosnipe_logic.py
from settings import SOLANA_WS_URL, PUMP_FUN_PROGRAM_ID_STR, solana_client
from utils import get_token_symbol_and_price # Assuming utils.py has this
# You'll need to pass the `db` object for models to work within a new thread
# or import it directly if `models.py` handles `db` initialization in a way that allows it.
# For now, we'll import `db` and models directly, assuming `db` is already initialized in `app.py`.
from models import db, Wallet, Trade, TradeLog, AutoSnipeConfig

load_dotenv()
API_KEY = os.getenv("API_KEY")
# Free tier users should use lite-api.jup.ag. api.jup.ag is for paid plans and requires an API key
API_BASE_URL = "https://api.jup.ag" if API_KEY else "https://lite-api.jup.ag"
# Set up headers for API requests (include x-api-key if API_KEY is available)
headers = {"x-api-key": API_KEY} if API_KEY else {}
# --- Global State for Autosniper ---
detected_tokens_queue = [] # Queue to hold detected tokens from listener
listener_running = False  # Flag to control the WebSocket listener's loop
processor_running = False # Flag to control the auto-buy processor's loop


# --- Logging Setup (for autosnipe_logic.py) ---
logger = logging.getLogger(__name__)

# These are handlers that will be managed by `app.py`'s `init_logging_handlers`
# We use global variables for the list_log_messages and db instance, which will be set by app.py
_list_log_messages_ref = None
_db_instance_ref = None

class ListLogHandler(logging.Handler):
    """Custom log handler to store logs in a list for the Flask UI."""
    def __init__(self, log_list_ref):
        super().__init__()
        self.log_list_ref = log_list_ref # Reference to the global list in app.py
        self.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    def emit(self, record):
        if self.log_list_ref is not None:
            log_entry = self.format(record)
            self.log_list_ref.append(log_entry)
            if len(self.log_list_ref) > 100: # Keep only last 100 logs
                self.log_list_ref.pop(0)

class DBLogHandler(logging.Handler):
    """Custom log handler to store logs in the database."""
    def __init__(self, db_instance_ref):
        super().__init__()
        self.db_instance_ref = db_instance_ref # Reference to the db instance from app.py
        self.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    def emit(self, record):
        if self.db_instance_ref is not None and self.db_instance_ref.app is not None:
            # Ensure operation within Flask app context for SQLAlchemy
            with self.db_instance_ref.app.app_context():
                log_entry = TradeLog(level=record.levelname, message=self.format(record))
                try:
                    self.db_instance_ref.session.add(log_entry)
                    self.db_instance_ref.session.commit()
                except Exception as e:
                    # Log to console if DB write fails, and rollback
                    logger.error(f"Error logging to DB (handler): {e}", exc_info=True)
                    self.db_instance_ref.session.rollback()

def init_logging_handlers(app_instance, db_instance, ui_log_list):
    """
    Initializes and adds logging handlers to the module's logger.
    Called by app.py to set up shared logging.
    """
    global _list_log_messages_ref, _db_instance_ref
    _list_log_messages_ref = ui_log_list
    _db_instance_ref = db_instance # Store reference to the db instance

    # Clear existing handlers to prevent duplicates on hot-reload/restart
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    list_handler = ListLogHandler(_list_log_messages_ref)
    list_handler.setLevel(logging.INFO)
    logger.addHandler(list_handler)

    db_handler = DBLogHandler(_db_instance_ref)
    db_handler.setLevel(logging.INFO)
    logger.addHandler(db_handler)
    logger.info("Autosnipe logging handlers initialized.")


def set_listener_running_status(status: bool):
    """Sets the running status for the WebSocket listener."""
    global listener_running
    listener_running = status
    if status:
        logger.info("Listener set to START.")
    else:
        logger.info("Listener set to STOP.")


def set_processor_running_status(status: bool):
    """Sets the running status for the auto-buy processor."""
    global processor_running
    processor_running = status
    if status:
        logger.info("Processor set to START.")
    else:
        logger.info("Processor set to STOP.")


# --- Helper Functions (Re-used/Adapted from previous responses) ---

def fetch_autosnipe_config():
    """Fetch user-defined AutoSnipeConfig parameters."""
    config = AutoSnipeConfig.query.first()  # Fetch the first config, or implement multi-config handling
    return {
        "min_txns": config.min_txns,
        "buy_txns_over_80_usd": config.buy_txns_over_80_usd,
        "launch_delay": config.launch_delay,
        "buy_amount": config.buy_amount,
        "slippage_percentage": config.slippage,
        "priority_fee_microlamports_per_cu": config.priority_fee * 1_000_000,  # Convert to micro-lamports
    }
# Ensure PUMP_FUN_PROGRAM_ID is a Pubkey object
PUMP_FUN_PROGRAM_ID = Pubkey.from_string(PUMP_FUN_PROGRAM_ID_STR)

async def solana_logs_listener():
    """
    Listens to Solana WebSocket for new token creations by monitoring specific program logs.
    Targets the pump.fun program ID for token mint events.
    """
    global detected_tokens_queue, listener_running

    if not SOLANA_WS_URL:
        logger.error("SOLANA_WS_URL is not set in environment variables. Cannot start listener.")
        set_listener_running_status(False)
        return

    logger.info(f"Connecting to Solana WebSocket: {SOLANA_WS_URL}")
    set_listener_running_status(True)

    try:
        # Use websockets.connect directly
        async with connect(SOLANA_WS_URL) as ws:
            # Construct the JSON-RPC request for logsSubscribe
            subscribe_request = {
                "jsonrpc": "2.0",
                "id": 1,  # Arbitrary request ID
                "method": "logsSubscribe",
                "params": [
                    {
                        "mentions": [str(PUMP_FUN_PROGRAM_ID)]  # Filter by program ID
                    },
                    {
                        "commitment": "processed"  # Listen for logs as they are processed
                    }
                ]
            }
            await ws.send(json.dumps(subscribe_request))

            # Read the subscription response
            response = await ws.recv()
            subscription_data = json.loads(response)
            subscription_id = subscription_data.get("result")
            if subscription_id:
                logger.info(f"Subscribed to logs with ID: {subscription_id}")
            else:
                logger.error(f"Failed to subscribe to logs: {subscription_data}")
                set_listener_running_status(False)
                return

            async for message in ws:
                if not listener_running:
                    logger.info("Listener stopping as requested.")
                    if subscription_id:
                        # Send unsubscribe message
                        unsubscribe_request = {
                            "jsonrpc": "2.0",
                            "id": 2,  # Arbitrary request ID
                            "method": "logsUnsubscribe",
                            "params": [subscription_id]
                        }
                        await ws.send(json.dumps(unsubscribe_request))
                    break

                data = json.loads(message)

                if "params" in data and "result" in data["params"] and "value" in data["params"]["result"]:
                    logs_data = data["params"]["result"]["value"]
                    signature = logs_data.get("signature")
                    logs = logs_data.get("logs", [])

                    for log_line in logs:
                        # CRITICAL: This log pattern is specific to pump.fun 'Create' instruction.
                        # Actual decoding requires understanding the Anchor IDL for pump.fun.
                        # This is a very basic attempt to identify the line.
                        if "Instruction: Create" in log_line and "Program data:" in log_line:
                            logger.info(f"Potential new token creation detected! Signature: {signature}")
                            try:
                                # For pump.fun, the mint address is often one of the accounts in the transaction
                                # or derived from the instruction data.
                                # To get the actual mint address reliably from a log, you would
                                # typically parse the transaction details that this log belongs to.
                                # However, fetching full transaction immediately adds latency.
                                # A true sniper would either predict the mint address or use a highly optimized
                                # way to get it from the log itself or transaction context.

                                # Placeholder: For this example, we'll assume the first detected
                                # instruction for the PUMP_FUN_PROGRAM_ID implies a new token launch,
                                # and we'll pass the signature to the processor. The processor will then
                                # attempt to find the token address by fetching transaction details.
                                # This adds latency, but is more reliable than guessing from raw logs.

                                # This is a temporary solution. For a robust `pump.fun` sniper,
                                # you need to correctly parse the `Program data:` to extract the `mint` address.
                                # For example, by looking at an Anchor IDL or reverse-engineering `pump.fun`'s instruction data.
                                # This will be the actual mint address from the log:
                                detected_mint_address = None
                                # A very simple and potentially fragile way to look for a pubkey in the log
                                # This needs to be replaced by proper IDL decoding for pump.fun.
                                for part in log_line.split(' '):
                                    try:
                                        test_pubkey = Pubkey.from_string(part.strip())
                                        if test_pubkey != PUMP_FUN_PROGRAM_ID: # Exclude the program ID itself
                                            detected_mint_address = str(test_pubkey)
                                            break
                                    except Exception:
                                        pass

                                if detected_mint_address:
                                    token_info = {
                                        "address": detected_mint_address, # This should be the actual mint address
                                        "tx_signature": signature,
                                        "launch_time": time.time(),
                                    }
                                    detected_tokens_queue.append(token_info)
                                    logger.info(f"Added new token (might be temp address) to queue: {token_info['address']}")
                                    if len(detected_tokens_queue) > 20: # Limit queue size
                                        detected_tokens_queue.pop(0)
                                else:
                                    logger.warning(f"Could not reliably extract mint address from log line: {log_line}. Signature: {signature}")


                            except Exception as e:
                                logger.error(f"Error processing detected token log: {e}", exc_info=True)
                else:
                    # Handle other WebSocket messages like pings/heartbeats
                    pass

    except (asyncio.CancelledError, Exception) as e:
        logger.error(f"Solana WebSocket listener error: {e}", exc_info=True)
    finally:
        set_listener_running_status(False)
        logger.info("Solana WebSocket listener stopped.")


def get_token_transactions(token_address: str, min_txns: int, min_value_usd: float) -> list:
    """
    Fetches recent transactions for a given token address and filters them
    based on minimum transaction count and USD value.
    This function will add latency due to RPC calls.

    Args:
        token_address: The mint address of the token.
        min_txns: Minimum number of transactions required.
        min_value_usd: Minimum USD value for each transaction to be counted.

    Returns:
        A list of transaction dictionaries if conditions are met, otherwise an empty list.
    """
    try:
        # Step 1: Get recent confirmed signatures for the token address
        signatures_resp = solana_client.get_signatures_for_address(
            Pubkey.from_string(token_address), limit=20 # Limit to a reasonable number
        )
        signatures = [str(s.signature) for s in signatures_resp.value]

        if not signatures:
            logger.info(f"No recent signatures found for {token_address} for transaction filtering.")
            return []

        logger.info(f"Checking {len(signatures)} recent signatures for {token_address} for transaction filtering...")

        valid_transactions = []
        for signature in signatures:
            # Step 2: Fetch full parsed transaction details for each signature
            # This is slow as it's a separate RPC call per signature.
            tx_details_resp = solana_client.get_parsed_transaction(signature, commitment=Confirmed, max_supported_transaction_version=0)
            tx_details = tx_details_resp.value

            if not tx_details or not tx_details.meta:
                continue

            # Calculate transaction value (USD)
            tx_value_usd = 0.0

            # Option A: Look for SOL transfers (common for initial buys/liquidity adds)
            for account_index, pre_balance in enumerate(tx_details.meta.pre_balances):
                post_balance = tx_details.meta.post_balances[account_index]
                sol_change = (post_balance - pre_balance) / (10**9) # Convert lamports to SOL
                if abs(sol_change) > 0.000000001: # Check for significant SOL change
                    sol_price_info = get_token_symbol_and_price("So11111111111111111111111111111111111111112") # Fetch SOL price
                    tx_value_usd += abs(sol_change) * sol_price_info['usdPrice']

            # Option B: Look for token balance changes (more direct for token trades)
            # This is complex for new tokens, as their USD price is volatile/unknown
            if tx_details.meta.token_balances:
                for token_balance in tx_details.meta.token_balances:
                    if str(token_balance.mint) == token_address:
                        # This assumes ui_amount represents the effective change.
                        # For a new token, its USD value is primarily driven by the SOL side of the LP.
                        # You'd ideally look at the instruction's amount.
                        # For the filter, let's assume the SOL value captures the essence.
                        pass # No direct USD value calculation from token_balance changes here to avoid circular logic with get_token_symbol_and_price on new tokens.

            if tx_value_usd >= min_value_usd:
                valid_transactions.append({'value': tx_value_usd, 'signature': signature})
                logger.info(f"  - Tx {signature[:8]}... passed value filter: ${tx_value_usd:.2f}")

            if len(valid_transactions) >= min_txns:
                break # Stop early if enough transactions are found

        if len(valid_transactions) >= min_txns:
            total_valid_value = sum(tx['value'] for tx in valid_transactions)
            logger.info(f"Conditions met for {token_address}: Found {len(valid_transactions)} valid transactions (total value ${total_valid_value:.2f}).")
            return valid_transactions
        else:
            logger.info(f"Conditions NOT met for {token_address}: Only found {len(valid_transactions)} valid transactions (min {min_txns}) or total value below threshold.")
            return []

    except Exception as e:
        logger.error(f"Error in get_token_transactions for {token_address}: {e}", exc_info=True)
        return []


def auto_buy_processor():
    """
    Processes new tokens detected by the listener and attempts to buy them
    based on the defined conditions. This runs in a continuous loop.
    """
    global detected_tokens_queue, processor_running
    set_processor_running_status(True)
    logger.info("Auto-buy processor started.")

    while processor_running or detected_tokens_queue: # Keep running if flag is true or queue has items
        try:
            if detected_tokens_queue:
                token_info = detected_tokens_queue.pop(0) # Get the next token from the queue
                token_address = token_info.get('address')
                tx_signature = token_info.get('tx_signature')
                launch_time = token_info.get('launch_time')

                if not token_address or not tx_signature or not launch_time:
                    logger.warning(f"Invalid token data in queue: {token_info}, skipping.")
                    continue

                logger.info(f"Attempting to process token: {token_address} (Source TX: {tx_signature[:8]}...)")

                # Fetch AutoSnipeConfig from DB
                config = fetch_autosnipe_config()

                # Condition 1: Check launch_delay (Time from Launch (sec))
                current_time = time.time()
                time_since_launch = current_time - launch_time
                if time_since_launch > config["launch_delay"]:
                    logger.info(f"Skipping {token_address}: Launch delay exceeded ({time_since_launch:.2f}s vs {config['launch_delay']}s allowed).")
                    continue

                # Condition 2 & 3: Check min transactions and min transaction value (Buy Txns > USD & Minimum Buy Txns Time)
                # WARNING: This step can add significant latency due to multiple RPC calls to fetch transaction details.
                # For very aggressive sniping, this might need optimization or re-evaluation.
                logger.info(f"Checking transaction conditions for {token_address}: Min Txns={config['min_txns']}, Min Value=${config['buy_txns_over_80_usd']}.")
                transactions_for_filter = get_token_transactions(
                    token_address,
                    config["min_txns"],
                    config["buy_txns_over_80_usd"]
                )

                if not transactions_for_filter: # If conditions not met (empty list)
                    logger.info(f"Skipping {token_address}: Did not meet min transaction count or value criteria.")
                    continue

                # All conditions met, proceed with buying
                logger.info(f"All conditions met for {token_address}. Proceeding to buy {config['buy_amount']} SOL worth.")

                # Fetch the wallet from DB (assuming one active wallet or specific wallet handling)
                wallet = Wallet.query.first()
                if not wallet:
                    logger.error("No wallet configured in DB for buying, skipping.")
                    continue

                private_key_path = wallet.private_key
                if not os.path.exists(private_key_path):
                    logger.error(f"Private key file not found at {private_key_path}, skipping buy.")
                    continue

                # Load private key
                try:
                    with open(private_key_path, 'rb') as key_file:
                        private_key_bytes = key_file.read()
                        if len(private_key_bytes) == 64:
                            wallet_keypair = SoldersKeypair.from_bytes(private_key_bytes)
                        elif len(private_key_bytes) == 32:
                            wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)
                        else:
                            raise ValueError(f"Private key file length {len(private_key_bytes)} is not 32 or 64 bytes.")
                    logger.info("Wallet keypair loaded successfully.")
                except Exception as e:
                    logger.error(f"Failed to load keypair from {private_key_path}: {e}", exc_info=True)
                    continue

                amount_in_lamports = int(config["buy_amount"] * 10**9) # Convert SOL to lamports

                # --- Jupiter Aggregator Swap Request ---

                quote_params = {
                    "inputMint": "So11111111111111111111111111111111111111112", # SOL Mint Address
                    "outputMint": token_address, # The new token
                    "amount": amount_in_lamports,
                    "slippageBps": int(config["slippage_percentage"] * 100) # Slippage in basis points
                }

                quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
                logger.info(f"Fetching Jupiter quote for {token_address} with {config['buy_amount']} SOL...")
                quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)

                if quote_response.status_code != 200:
                    logger.error(f"Error fetching Jupiter quote: {quote_response.text}")
                    continue

                quote_data = quote_response.json()
                logger.info(f"Jupiter quote received. Output amount: {quote_data.get('outAmount')} {quote_data.get('outputMint')}")

                swap_request_payload = {
                    "userPublicKey": str(wallet_keypair.pubkey()),
                    "quoteResponse": quote_data,
                    "computeUnitPriceMicroLamports": config["priority_fee_microlamports_per_cu"],
                    "wrapUnwrapSOL": True
                }

                swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
                logger.info(f"Requesting Jupiter swap for {token_address}...")
                swap_response = requests.post(swap_endpoint, json=swap_request_payload, headers=headers)

                if swap_response.status_code != 200:
                    logger.error(f"Error performing Jupiter swap: {swap_response.text}")
                    continue

                swap_data = swap_response.json()
                swap_transaction_base64 = swap_data.get("swapTransaction")
                if not swap_transaction_base64:
                    logger.error(f"No swapTransaction found in Jupiter response.")
                    continue

                swap_transaction_bytes = base64.b64decode(swap_transaction_base64)
                raw_transaction = VersionedTransaction.from_bytes(swap_transaction_bytes)

                # Sign the transaction
                try:
                    signed_transaction = wallet_keypair.sign_versioned_transaction(raw_transaction)
                    logger.info("Transaction signed successfully.")
                except Exception as e:
                    logger.error(f"Error signing transaction for {token_address}: {e}", exc_info=True)
                    continue

                # Send the signed transaction
                try:
                    rpc_response = solana_client.send_transaction(signed_transaction)
                    signature = str(rpc_response.value)
                    logger.info(f"Buy transaction sent successfully! Signature: {signature}")

                    # Store executed trade in database
                    executed_trade = Trade(
                        trade_type="BUY",
                        token_address=token_address,
                        amount=config["buy_amount"], # Amount of SOL spent for the trade
                        tx_id=signature
                    )
                    db.session.add(executed_trade)
                    db.session.commit()
                    logger.info(f"Trade recorded in DB for {token_address}: {signature}")

                except Exception as e:
                    logger.error(f"Error sending buy transaction for {token_address}: {e}", exc_info=True)

            else:
                # No tokens in queue, wait a bit before checking again
                time.sleep(0.1) # Poll queue frequently to minimize latency

        except Exception as e:
            logger.error(f"Unexpected error in auto_buy_processor loop: {str(e)}", exc_info=True)
            time.sleep(1) # Wait a bit longer on error to prevent busy loop
    set_processor_running_status(False) # Set to stopped when loop finishes


def start_background_jobs():
    # Start the token detection listener in a separate thread
    listener_thread = threading.Thread(target=asyncio.run, args=(solana_logs_listener(),), daemon=True)
    listener_thread.start()
    logger.info("Token detection listener started in background.")

    # Start the auto-buy processor in another thread
    processor_thread = threading.Thread(target=auto_buy_processor, daemon=True)
    processor_thread.start()
    logger.info("Auto-buy processor started in background.")

    # Ensure threads shut down gracefully on app exit
    atexit.register(lambda: set_listener_running_status(False))
    atexit.register(lambda: set_processor_running_status(False))

