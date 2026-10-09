import logging
import os
import atexit
import time

from dotenv import load_dotenv
from models import db, Wallet, Trade, TradeLog, TradeHistory
import requests
import base64
from flask import Blueprint
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from settings import solana_client
from shyft_pricing import get_token_price
from yellowstone_pricing import YellowstonePricingError, get_yellowstone_price, is_yellowstone_enabled
from solana.rpc.types import TokenAccountOpts
from solders.pubkey import Pubkey
from apscheduler.schedulers.background import BackgroundScheduler
from autosnipe_sell_logic import evaluate_autosnipe_sell

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


def get_sniper_sell_price(token_address: str) -> dict:
    """
    Latest price for a Sniper sell decision.

    Yellowstone when enabled (USE_GEYSER=true + GEYSER_GRPC_URL set); falls
    back to the existing Shyft path only while Yellowstone is
    disabled/unconfigured, so today's behavior is unchanged until it's
    turned on.

    Yellowstone's accountSubscribe only ever delivers a price while the
    token is actively trading - a token that has gone quiet (no more
    buys/sells) will time out on every single cycle forever, since there is
    no new account update for it to push. When that happens, fall back to a
    Shyft RPC snapshot read (a point-in-time account fetch, not a
    subscription, so it works regardless of trading activity) instead of
    skipping the trade indefinitely.
    """
    if is_yellowstone_enabled():
        try:
            price_data = get_yellowstone_price(token_address)
            print(
                "[Sell Price][Yellowstone/Geyser] Successfully retrieved "
                f"token={token_address} usd_price={price_data.get('usdPrice')} "
                f"source={price_data.get('source', 'Yellowstone/Geyser')}",
                flush=True,
            )
            return price_data
        except YellowstonePricingError as exc:
            logger.warning(
                f"Yellowstone price fetch failed for {token_address} "
                f"({exc}); falling back to Shyft RPC snapshot."
            )
            return get_token_price(token_address)
    return get_token_price(token_address)


def get_wallet_token_balance(owner: Pubkey, mint: str) -> tuple[int, int | None]:
    """Raw amount and decimals the wallet actually holds for mint (all its token accounts, any token program)."""
    resp = solana_client.get_token_accounts_by_owner_json_parsed(
        owner, TokenAccountOpts(mint=Pubkey.from_string(mint))
    )
    raw, decimals = 0, None
    for acc in resp.value:
        token_amount = acc.account.data.parsed["info"]["tokenAmount"]
        raw += int(token_amount["amount"])
        decimals = token_amount["decimals"]
    return raw, decimals


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
                    # A failed/invalid Yellowstone fetch skips this trade for the
                    # current cycle rather than selling on a stale or guessed price.
                    try:
                        current_price_ = get_sniper_sell_price(trade_data.token_address)
                        current_price = current_price_['usdPrice']
                    except YellowstonePricingError as e:
                        logger.warning(f"Yellowstone price fetch failed for trade {trade_data.id}: {e}")
                        continue
                    except Exception as e:
                        logger.warning(f"Failed to fetch price for trade {trade_data.id}: {e}")
                        continue
                    if not current_price or current_price <= 0:
                        continue
                    initial_price = trade_data.initial_price
                    if initial_price <= 0:
                        logger.warning(f"Invalid initial price for trade {trade_data.id}. Skipping auto-snipe.")
                        continue

                    decision = evaluate_autosnipe_sell(trade_data, current_price, price_tracking)
                    amount_to_trade, message = decision.amount, decision.message
                    if amount_to_trade <= 0:
                        continue
                    trade_ref = f"trade {trade_data.id} ({trade_data.token_address})"
                    logger.info(
                        f"[Sell decision] {trade_ref}: {message} | "
                        f"price={current_price} x{current_price / initial_price:.3f} amount={amount_to_trade}"
                    )
                    try:
                        # Continue with the same logic for performing the trade
                        wallet = Wallet.query.filter_by(public_key=trade_data.to_pubkey).first()
                        if not wallet:
                            logger.warning(f"Wallet not found for {trade_data.to_pubkey}, skipping {trade_ref}.")
                            continue
                        private_key_path = wallet.private_key
                        if not os.path.exists(private_key_path):
                            # Must not `return` here: this runs inside the scheduler
                            # loop, so returning silently aborted the whole cycle and
                            # every older trade after this one was never evaluated.
                            logger.error(f"Private key file missing for wallet {wallet.public_key}, skipping {trade_ref}.")
                            continue

                        with open(private_key_path, 'rb') as key_file:
                            private_key_bytes = key_file.read()
                        if len(private_key_bytes) == 32:
                            wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)
                        elif len(private_key_bytes) == 64:
                            wallet_keypair = SoldersKeypair.from_bytes(private_key_bytes)
                        else:
                            logger.error(
                                f"Invalid private key length ({len(private_key_bytes)}) for trade {trade_data.id}, skipping."
                            )
                            continue

                        try:
                            raw_balance, decimals = get_wallet_token_balance(
                                wallet_keypair.pubkey(), trade_data.token_address
                            )
                        except Exception as e:
                            logger.warning(f"Could not read wallet token balance for {trade_ref}: {e}")
                            continue
                        if raw_balance <= 0:
                            logger.warning(
                                f"Wallet holds 0 tokens for {trade_ref} "
                                f"(DB still shows {trade_data.purchased_token_amount}); skipping sell."
                            )
                            continue

                        # purchased_token_amount is the buy quote's estimate, not what
                        # actually arrived (usually a few % less). Selling the stored
                        # amount on a full exit made Jupiter fail with InsufficientFunds
                        # (0x1788) on every cycle, so full exits sell the real balance
                        # and partial sells are capped by it.
                        full_exit = amount_to_trade >= trade_data.purchased_token_amount
                        requested_raw = int(amount_to_trade * (10 ** decimals))
                        amount_in_lamports = raw_balance if full_exit else min(requested_raw, raw_balance)
                        sold_tokens = amount_in_lamports / (10 ** decimals)

                        quote_params = {
                            "inputMint": trade_data.token_address,
                            "outputMint": "So11111111111111111111111111111111111111112",
                            "amount": amount_in_lamports,
                            "slippageBps": int(trade_data.autosnipe_sell_slippage * 100)
                        }

                        quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
                        quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers, timeout=15)
                        if quote_response.status_code != 200:
                            logger.error(f"Error fetching quote for {trade_ref}: {quote_response.json()}")
                            continue

                        quote_data = quote_response.json()
                        logger.info(f"Quote data: {quote_data}")

                        swap_request = {
                            "userPublicKey": str(wallet_keypair.pubkey()),
                            "quoteResponse": quote_data,
                            "computeUnitPriceMicroLamports": int(trade_data.default_gas_fee * 1_000_000),
                            "wrapUnwrapSOL": True
                        }

                        swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
                        swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers, timeout=15)
                        if swap_response.status_code != 200:
                            logger.error(f"Error performing swap for {trade_ref}: {swap_response.json()}")
                            continue

                        swap_data = swap_response.json()
                        swap_transaction_base64 = swap_data["swapTransaction"]
                        swap_transaction_bytes = base64.b64decode(swap_transaction_base64)
                        raw_transaction = VersionedTransaction.from_bytes(swap_transaction_bytes)

                        account_keys = raw_transaction.message.account_keys
                        wallet_index = account_keys.index(wallet_keypair.pubkey())
                        signers = list(raw_transaction.signatures)
                        signers[wallet_index] = wallet_keypair
                        signed_transaction = VersionedTransaction(raw_transaction.message, signers)

                        try:
                            rpc_response = solana_client.send_transaction(signed_transaction)
                            signature = str(rpc_response.value)
                            logger.info(f"{message}View transaction on Solscan: https://solscan.io/tx/{signature}")
                            logger.info(f"Transaction sent successfully! Signature: {signature}")
                            print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")

                            executed_trade = TradeHistory(
                                trade_id=trade_data.id,
                                token_address=trade_data.token_address,
                                trade_type="SELL",
                                trade_kind="AutoSnipe",
                                amount=sold_tokens,
                                execution_price=current_price if current_price else 0,
                                tx_id=signature
                            )
                            db.session.add(executed_trade)
                            db.session.commit()
                            if full_exit:
                                trade_data.purchased_token_amount = 0
                            else:
                                trade_data.purchased_token_amount -= amount_to_trade
                            if trade_data.purchased_token_amount <= 0:
                                trade_data.executed = True
                            if decision.take_profit_tier is not None:
                                trade_data.last_tp_tier = decision.take_profit_tier
                            db.session.commit()
                        except Exception as e:
                            logger.error(f"Error sending transaction for {trade_ref}: {str(e)}")

                    except Exception as e:
                        logger.error(f"Auto-snipe sell error for {trade_ref}: {str(e)}")
                        db.session.rollback()
                print("autosnipe token sell successfully executed")
                return None
            except Exception as e:
                db.session.rollback()
                logger.error(f"Unexpected error in auto_trade loop: {e}")
                time.sleep(1)

    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_snipe_sell, 'interval', seconds=5)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
