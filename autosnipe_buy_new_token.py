import os
import struct
import time
import logging
from datetime import datetime
from models import db, Wallet, Trade, TradeHistory, AutoSnipeConfig, TradeLog
import requests
import base64
from flask import jsonify, Blueprint
from dotenv import load_dotenv
from requests import JSONDecodeError
from solana.rpc.commitment import Confirmed
from solana.rpc.core import RPCException
from solana.rpc.types import TxOpts
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus
from settings import (
    solana_client,
    SNIPER_USE_WEBSOCKET,
    SNIPER_HTTP_FALLBACK,
    SNIPER_BUY_RECONCILE_INTERVAL_SEC,
    SNIPER_BUY_RECONCILE_TIMEOUT_SEC,
    SNIPER_BUY_SOL_RESERVE_SOL,
)
from shyft_pricing import get_token_price, get_shyft_pricing_service
from solders.pubkey import Pubkey
from latency_log import StageTimer
from sqlalchemy.exc import IntegrityError

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

# Pyth SOL/USD price feed account (mainnet-beta). Read directly via our own
# solana_client - a plain point-in-time getAccountInfo, not a subscription,
# so none of Yellowstone/Geyser's "must wait for a live push" unreliability
# applies, and it doesn't go through Shyft's RPC either. Used only for the
# buy-time entry-price calculation; sell-side pricing is untouched.
PYTH_SOL_USD_PRICE_FEED = Pubkey.from_string("7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE")


def get_sol_usd_rate() -> float | None:
    """
    Direct, first-party SOL/USD rate for the entry-price calculation: reads
    Pyth's SOL/USD account once via solana_client. Returns None (never
    raises) on any failure, so callers can fall back safely without
    crashing the buy or the reconciliation job.
    """
    try:
        resp = solana_client.get_account_info(PYTH_SOL_USD_PRICE_FEED, commitment=Confirmed)
        raw = resp.value.data if resp.value else None
    except Exception as exc:
        logger.warning(f"SOL/USD Pyth read failed: {exc}")
        return None
    if not raw or len(raw) < 93:
        return None
    price_data = struct.unpack_from("<q", raw, 73)[0]
    exponent = struct.unpack_from("<i", raw, 89)[0]
    value = float(price_data) * (10.0 ** float(exponent))
    return value if value > 0 else None


from threading import Lock

WALLET_BUY_LOCK = Lock()
# (user_id, mint) pairs a buy transaction was sent (or attempted) for. Checked
# and updated under WALLET_BUY_LOCK, so the same user never buys a mint twice.
BOUGHT_TOKENS = set()
# Preflight against the confirmed bank: the client's default (finalized) is
# ~30 slots behind, where a just-launched token and Jupiter's recent blockhash
# don't exist yet, so preflight rejected buys of new tokens.
BUY_TX_OPTS = TxOpts(preflight_commitment=Confirmed)


def mark_autosnipe_position_closed(user_id, token_address) -> None:
    """
    Clears the in-memory buy-dedup fast path for (user_id, token_address)
    the moment a position is fully closed. Action-plan 1.3 scoped the
    buy-dedup check (DB index below, and the query check in
    _buy_token_internal) to open positions only, not forever - without this,
    BOUGHT_TOKENS would keep shadowing that and block a legitimate rebuy of
    a fully-sold mint until the process restarts and the in-memory set
    happens to get cleared. Called from autosnipe_sell_script.py wherever an
    AUTOSNIPE trade's `executed` flips to True.
    """
    BOUGHT_TOKENS.discard((user_id, token_address))


def describe_send_error(exc: Exception) -> str:
    """Readable reason for a failed send_transaction (any RPC error shape)."""
    err = exc.args[0] if exc.args else exc
    message = getattr(err, "message", None) or str(err)
    data = getattr(err, "data", None)
    detail = getattr(data, "err", None)
    logs = getattr(data, "logs", None) or []
    parts = [str(message)]
    if detail is not None:
        parts.append(f"err={detail}")
    failing = [line for line in logs if "rror" in line or "failed" in line]
    if failing:
        parts.append(f"log={failing[-1]}")
    return " | ".join(parts)


def get_token_price_usd(token_address):
    try:
        current_token_price_ = get_token_price(token_address)
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
    current_sol_price = get_token_price(token_address)  # Shyft live price
    return sol_amount * float(current_sol_price.get("usdPrice") or 0)


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


# def should_buy_token(token_address: str, user_id: int) -> bool:
def should_buy_token(token_address, user_id, config):

    """
    Checks if a token meets the buy conditions from AutoSnipeConfig for a user.
    Phase 1: uses WebSocket stream counters when the sniper WS listener is active.
    Falls back to HTTP transaction fetching when WebSocket is disabled.

    Optional half-txn extended scan (config.half_txns_scan_enabled):
    if the normal launch_delay window ends with qualifying txs in
    [min_txns/2, min_txns), continue scanning for an additional
    half_txns_scan_duration seconds (added in full on top of launch_delay).
    """
    from autosnipe_buy_logic import (
        extended_scan_deadline,
        should_start_half_txn_extended_scan,
    )

    # config = AutoSnipeConfig.query.filter_by(user_id=user_id, active=True).first()
    if not config:
        print(f"  [Buy Condition] No active AutoSnipeConfig found for user {user_id}. Cannot determine buy conditions.")
        return False

    check_duration = config.launch_delay  # seconds
    half_enabled = bool(getattr(config, "half_txns_scan_enabled", False))
    half_duration = getattr(config, "half_txns_scan_duration", 30) or 30
    print(
        f"  [Buy Condition] Checking buy conditions for {token_address} for user {user_id} "
        f"for max {check_duration} seconds..."
    )

    def _maybe_extend(start_time, qualifying, path_label: str):
        if not should_start_half_txn_extended_scan(
            enabled=half_enabled,
            qualifying_count=qualifying,
            min_txns=config.min_txns,
        ):
            return None
        deadline = extended_scan_deadline(start_time, check_duration, half_duration)
        remaining = deadline - time.time()
        if remaining <= 0:
            print(
                f"[Buy Condition][{path_label}] Half-txn extended scan configured "
                f"but deadline already passed (additional={half_duration}s)."
            )
            return None
        print(
            f"[Buy Condition][{path_label}] Qualifying={qualifying} >= half of "
            f"{config.min_txns}; extending scan for an additional {half_duration}s "
            f"(total window {check_duration + half_duration}s)."
        )
        return deadline

    # --- WebSocket path: read in-memory counters (no HTTP polling) ---
    try:
        from sniper_stream import get_mint_buy_stats, is_ws_active

        if is_ws_active():
            start_time = time.time()
            last_qualifying = 0
            stats = {}  # guards the NOT-MET log line below if check_duration <= 0
            while time.time() - start_time < check_duration:
                stats = get_mint_buy_stats(token_address, config.buy_txns_over_80_usd)
                last_qualifying = stats["qualifying_count"]
                print(
                    f"[Buy Condition][WS] qualifying={last_qualifying} / required={config.min_txns} "
                    f"(total buys={stats['total_buys']})"
                )
                if last_qualifying >= config.min_txns:
                    print(
                        f"[Buy Condition][WS] Buy conditions MET for {token_address}: "
                        f"{last_qualifying} transactions >= ${config.buy_txns_over_80_usd}."
                    )
                    return True
                time.sleep(0.2)

            extend_deadline = _maybe_extend(start_time, last_qualifying, "WS")
            if extend_deadline is not None:
                while time.time() < extend_deadline:
                    stats = get_mint_buy_stats(token_address, config.buy_txns_over_80_usd)
                    last_qualifying = stats["qualifying_count"]
                    print(
                        f"[Buy Condition][WS][Extended] qualifying={last_qualifying} / "
                        f"required={config.min_txns}"
                    )
                    if last_qualifying >= config.min_txns:
                        print(
                            f"[Buy Condition][WS][Extended] Buy conditions MET for {token_address}."
                        )
                        return True
                    time.sleep(0.2)

            print(
                f"[Buy Condition][WS] Buy conditions NOT MET for {token_address} within scan window. "
                f"(candidate txns seen={stats.get('candidates_seen', 0)}, "
                f"excluded={stats.get('candidates_excluded', 0)} [sell/failed/unresolved])"
            )
            return False
    except ImportError:
        pass

    # --- HTTP fallback (legacy polling) ---
    start_time = time.time()
    last_qualifying = 0
    while time.time() - start_time < check_duration:
        txns = get_token_specific_transactions(
            token_address, config.min_txns, config.buy_txns_over_80_usd
        )
        if not txns:
            time.sleep(1)
            continue

        last_qualifying = 0
        for txn in txns:
            usd_value = txn['usd_value'] if txn['usd_value'] else 0.0
            if usd_value >= config.buy_txns_over_80_usd:
                last_qualifying += 1
        print(f"[Buy Condition][HTTP] Current qualifying transactions: {last_qualifying} / required: {config.min_txns}.")

        if last_qualifying >= config.min_txns:
            print(f"[Buy Condition][HTTP] Buy conditions MET for {token_address}: {last_qualifying} transactions >= ${config.buy_txns_over_80_usd}.")
            return True
        time.sleep(1)

    extend_deadline = _maybe_extend(start_time, last_qualifying, "HTTP")
    if extend_deadline is not None:
        while time.time() < extend_deadline:
            txns = get_token_specific_transactions(
                token_address, config.min_txns, config.buy_txns_over_80_usd
            )
            last_qualifying = 0
            for txn in (txns or []):
                usd_value = txn['usd_value'] if txn['usd_value'] else 0.0
                if usd_value >= config.buy_txns_over_80_usd:
                    last_qualifying += 1
            print(
                f"[Buy Condition][HTTP][Extended] qualifying={last_qualifying} / "
                f"required={config.min_txns}."
            )
            if last_qualifying >= config.min_txns:
                print(f"[Buy Condition][HTTP][Extended] Buy conditions MET for {token_address}.")
                return True
            time.sleep(1)

    print(f"[Buy Condition][HTTP] Buy conditions NOT MET for {token_address} within scan window.")
    return False

def _delete_unsent_pending_trade(trade):
    """Removes a Trade row written before the swap/send once it's confirmed
    the send was never attempted (the quote succeeded but building/fetching
    the swap transaction failed). Only call this for a failure that happens
    strictly before send_transaction is ever invoked - once a send has been
    issued, a raised error doesn't rule out the transaction having landed,
    so the row must be kept instead (see the call site)."""
    try:
        db.session.delete(trade)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(f"Failed to clean up pending Trade {trade.id} after a swap setup failure.")


def buy_token(token_address, config, decision_ms=None):
    timer = StageTimer("buy", mint=token_address, user=getattr(config, "user_id", None))
    if decision_ms is not None:
        timer.add("decision", decision_ms)
    wait_start = time.monotonic()
    try:
        with WALLET_BUY_LOCK:
            timer.add("lock_wait", (time.monotonic() - wait_start) * 1000)
            return _buy_token_internal(token_address, config, timer)
    finally:
        timer.finish()

# def _buy_token_internal(token_address: str, user_id: int) -> bool:
def _buy_token_internal(token_address, config, timer=None):

    global signature
    timer = timer or StageTimer("buy", mint=token_address)
    try:
        # config = AutoSnipeConfig.query.filter_by(user_id=user_id, active=True).first()
        if not config:
            logger.error(f"[Buy Token] No active AutoSnipeConfig found for user. Cannot proceed with buy.")
            timer.outcome = "no_config"
            return True
        buy_key = (config.user_id, token_address)
        if buy_key in BOUGHT_TOKENS:
            logger.info(f"Token {token_address} already bought, skipping.")
            timer.outcome = "duplicate_skipped"
            return False
        # BOUGHT_TOKENS alone is in-memory only - a restart clears it, so the
        # same user could buy the same mint again. This DB check survives a
        # restart; kept in addition to (not instead of) the in-memory set
        # above, which is still useful as a fast path that avoids a query
        # for mints seen repeatedly within the same run.
        # executed=False (action-plan 1.3): scoped to open positions, not
        # forever - once a position is fully closed, the mint is tradable
        # again. Matches the DB-level unique index below and the
        # mark_autosnipe_position_closed() call on the sell side.
        if Trade.query.filter_by(
            user_id=config.user_id, token_address=token_address, auto_snipe=True, executed=False
        ).first():
            logger.info(f"Token {token_address} already has an open AUTOSNIPE trade for this user, skipping.")
            BOUGHT_TOKENS.add(buy_key)
            timer.outcome = "duplicate_skipped"
            return False

        # Fetch relevant settings from the AutoSnipeConfig
        # These are used for the actual swap execution parameters
        buy_amount_sol = config.buy_amount if config.buy_amount else 1.0
        buy_slippage_percent_bps = int((config.slippage if config.slippage else 20) * 100) # Convert percentage to BPS
        priority_fee_sol = config.priority_fee if config.priority_fee else 0.01

        # Prefer the wallet this sniper was explicitly configured with. Falls
        # back to the user's oldest wallet (deterministic, unlike .first()
        # with no order_by, which Postgres doesn't guarantee is stable) for
        # configs saved before wallet_id existed, or left unset.
        wallet = None
        config_wallet_id = getattr(config, "wallet_id", None)
        if config_wallet_id:
            wallet = Wallet.query.filter_by(id=config_wallet_id, user_id=config.user_id).first()
        if not wallet:
            wallet = Wallet.query.filter_by(user_id=config.user_id).order_by(Wallet.id.asc()).first()
        if not wallet:
            logger.error(f"Failed to fetch wallet for user.")
            timer.outcome = "no_wallet"
            return True
        to_pubkey = wallet.public_key

        amount = buy_amount_sol # Amount in SOL to spend

        # Shyft is intentionally not used anywhere on the buy's hot path -
        # token name/symbol are just set statically rather than fetched, per
        # explicit instruction to not depend on Shyft here at all (not even
        # for metadata, let alone pricing). reconcile_pending_buys() still
        # fills in the real entry price afterward from the confirmed
        # transaction itself, same as before.
        token_name = "Unknown"
        token_symbol = "UNKNOWN"

        # Entry price is NOT fetched here - no Geyser, no Shyft, no live
        # price of any kind on the buy's hot path. It's a placeholder
        # (autosnipe_sell_logic treats initial_price<=0 as "nothing to
        # evaluate yet", so this is always safe) until
        # reconcile_pending_buys() computes the real one from the confirmed
        # transaction's actual SOL spent / tokens received, a few seconds
        # after this function returns. See that function for why.
        current_token_price = 0.0
        estimated_tokens = 0 # Will be updated after quote


        # ✅ Check if wallet exists and balance is sufficient
        with timer.stage("balance"):
            wallet_balance_response = solana_client.get_balance(Pubkey.from_string(to_pubkey), commitment=Confirmed)
        wallet_balance = wallet_balance_response.value / 1_000_000_000  # Convert lamports to SOL

        # Requires a reserve on top of the buy amount - rent, network/priority
        # fees, and SOL the wallet will need to pay for its own later sell -
        # not just enough to cover the buy itself. Otherwise the bot can spend
        # a wallet down to a balance too low to ever sell from again.
        required = amount + SNIPER_BUY_SOL_RESERVE_SOL
        if wallet_balance < required:
            logger.error(
                f"Insufficient balance. Wallet has {wallet_balance:.4f} SOL, but {required:.4f} SOL is "
                f"required ({amount:.4f} buy + {SNIPER_BUY_SOL_RESERVE_SOL:.4f} reserve)."
            )
            timer.outcome = "insufficient_sol"
            return True

        private_key_path = wallet.private_key
        if not os.path.exists(private_key_path):
            logger.error(f"Failed to fetch private key")
            timer.outcome = "no_key"
            return True

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
        with timer.stage("quote"):
            quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers, timeout=15)

        if quote_response.status_code != 200:
            timer.outcome = "quote_failed"
            try:
                print(f"Error fetching quote: {quote_response.json()}")
                logger.error(f"Error fetching quote")
                return True
            except JSONDecodeError:
                print(f"Error fetching quote: {quote_response.text}")
                logger.error(f"Error fetching quote (non-JSON response)")
                return True

        quote_data = quote_response.json()
        estimated_tokens = int(quote_data.get("outAmount", 0)) / (10 ** quote_data.get("outputMintDecimals", 6))

        # Write the Trade row now, before the swap is even built, instead of
        # after send_transaction succeeds (as this used to do). Every field
        # below is already known from the quote response - nothing here
        # depends on the send. This closes the window where a real on-chain
        # buy landed but the DB write that would have recorded it afterward
        # failed, leaving tokens in the wallet with no Trade row - never
        # recorded, never sellable by the bot. If the swap/send never
        # actually happens (quote succeeded but the swap step below fails),
        # this row is deleted again (see _delete_unsent_pending_trade below).
        # If send_transaction itself raises, the row is deliberately kept:
        # a raised error doesn't mean the transaction didn't land (see the
        # BOUGHT_TOKENS comment a few lines down), so deleting it there would
        # risk erasing the only record of a buy that actually went through.
        # A Trade row whose tokens never arrive is already handled gracefully
        # elsewhere - the sell cycle logs a zero-balance warning and skips it
        # rather than erroring - which is a far safer failure mode than real
        # money spent with no DB record at all.
        new_trade = Trade(
            user_id=int(config.user_id),
            config_id=None,
            token_address=token_address,
            token_name=token_name,
            token_symbol=token_symbol,
            initial_price=current_token_price,
            amount=amount, # Amount of SOL spent
            purchased_token_amount=estimated_tokens,
            initial_token_amount=estimated_tokens,
            trade_type="BUY",
            to_pubkey=to_pubkey,

            # Sell priority fee (SOL). Previously nothing ever set this, so
            # every snipe sell's priority fee was stuck at the column's own
            # hardcoded default regardless of what the config asked for.
            default_gas_fee=config.sell_priority_fee if getattr(config, "sell_priority_fee", None) else 0.01,

            # Autosnipe settings from the config
            auto_snipe=True, # This trade was initiated by autosnipe logic
            trade_kind="AUTOSNIPE", # Explicitly marking it as an AUTOSNIPE trade
            # Percent (e.g. 20 = 20%), same unit as config.slippage - this is
            # later multiplied by 100 to get bps (autosnipe_sell_script.py).
            # The old 0.30 fallback here was 0.30%, not the intended 30%.
            autosnipe_sell_slippage=config.slippage if config.slippage else 20,
            drop_cutoff=config.drop_cutoff if config.drop_cutoff else 30,
            drop_cutoff_enabled=bool(getattr(config, "drop_cutoff_enabled", True)),
            drop_until_profit=config.drop_until_profit if config.drop_until_profit else 99,
            drop_after_100=config.drop_after_100 if config.drop_after_100 else 50,
            drop_after_100_enabled=bool(getattr(config, "drop_after_100_enabled", True)),
            drop_after_400=config.drop_after_400 if config.drop_after_400 else 30,
            drop_after_400_enabled=bool(getattr(config, "drop_after_400_enabled", True)),
            sell_at_100=config.sell_at_100 if getattr(config, "sell_at_100", None) else 10,
            sell_at_200=config.sell_at_200 if config.sell_at_200 else 10,
            sell_at_300=config.sell_at_300 if getattr(config, "sell_at_300", None) else 10,
            sell_at_400=config.sell_at_400 if config.sell_at_400 else 10,
            sell_at_600=config.sell_at_600 if getattr(config, "sell_at_600", None) else 10,
            sell_at_800=config.sell_at_800 if getattr(config, "sell_at_800", None) else 10,
            sell_at_1000=config.sell_at_1000 if config.sell_at_1000 else 10,
            sell_at_1500=config.sell_at_1500 if config.sell_at_1500 else 10,
            sell_at_2500=config.sell_at_2500 if config.sell_at_2500 else 10,
            sell_at_4000=config.sell_at_4000 if config.sell_at_4000 else 10,
            sell_at_10000=config.sell_at_10000 if config.sell_at_10000 else 10,
            inactivity_sell_enabled=bool(getattr(config, "inactivity_sell_enabled", True)),
            inactivity_sell_seconds=(
                config.inactivity_sell_seconds if getattr(config, "inactivity_sell_seconds", None) else 5.0
            ),
        )
        with timer.stage("db_pending"):
            db.session.add(new_trade)
            try:
                db.session.commit()
            except IntegrityError:
                # Belt-and-suspenders for the query check above + WALLET_BUY_LOCK
                # (action-plan 1.3): the DB's own partial unique index is the
                # only thing that can't be raced, in case either of those
                # ever stop covering every path that reaches here.
                db.session.rollback()
                logger.info(f"Token {token_address} already has an open AUTOSNIPE trade (DB constraint), skipping.")
                BOUGHT_TOKENS.add(buy_key)
                timer.outcome = "duplicate_skipped"
                return False

        # Fetch the swap transaction for the quote
        swap_request = {
            "userPublicKey": str(wallet_keypair.pubkey()),
            "quoteResponse": quote_data,
            # "computeUnitPriceMicroLamports" is a price PER compute unit; the
            # old `* 1_000_000` conversion treated it as if it were the total
            # fee, so 0.01 SOL became ~0.000002 SOL actually spent.
            # "prioritizationFeeLamports" is the total lamports to spend on
            # priority, which is what a SOL-denominated setting actually means.
            "prioritizationFeeLamports": int(priority_fee_sol * 1_000_000_000),
            "wrapUnwrapSOL": True
        }

        swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
        with timer.stage("swap"):
            swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers, timeout=15)

        if swap_response.status_code != 200:
            timer.outcome = "swap_failed"
            _delete_unsent_pending_trade(new_trade)
            try:
                print(f"Error performing swap: {swap_response.json()}")
                logger.error(f"Error performing swap)")
                return True
            except JSONDecodeError:
                print(f"Error performing swap: {swap_response.text}")
                logger.error(f"Error performing swap (non-JSON response)")
                return True

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

        # Send the signed transaction to the RPC client. Marked as bought
        # first: a send that errors may still have landed, so it is never
        # repeated for this user and mint (there is no automatic resubmission).
        BOUGHT_TOKENS.add(buy_key)
        try:
            start_time = time.time()
            with timer.stage("send"):
                rpc_response = solana_client.send_transaction(signed_transaction, opts=BUY_TX_OPTS)
            end_time = time.time()
            execution_time_ms = (end_time - start_time) * 1000
            signature = str(rpc_response.value)
            timer.fields["sig"] = signature
            print(f"Auto trade job executed in {execution_time_ms:.2f} ms!")
            print(f"Transaction sent successfully! Signature: {signature}")
            print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")

        except RPCException as e:
            reason = describe_send_error(e)
            print(f"Transaction failed! {reason}")
            logger.error(f"Buy transaction for {token_address} rejected: {reason}")
            timer.outcome, timer.reason = "send_rejected", reason.split(" | ")[0][:80].replace(" ", "_")
            return True

        # new_trade was already written to the DB before the swap/send above
        # (see the comment there) - only the TradeHistory row, which needs
        # the real tx signature, waits until here.
        timer.outcome = "sent"
        with timer.stage("db"):
            # Record in TradeHistory. status="sent" (rather than the column's
            # default "confirmed") marks this row as awaiting
            # reconcile_pending_buys(), which confirms the transaction and
            # fills in the real execution_price/Trade.initial_price -
            # mirrors how the sell flow's own reconcile_pending_sells()
            # already uses this same column.
            executed_trade = TradeHistory(
                trade_id=new_trade.id,
                token_address=token_address,
                trade_type="BUY",
                trade_kind="AUTOSNIPE", # Consistent with Trade table
                amount=amount, # Amount of SOL spent
                execution_price=current_token_price,
                tx_id=signature,
                status="sent",
            )
            db.session.add(executed_trade)
            db.session.commit()
        logger.info(f"AutoSnipe Trade against token address {token_address} run successfully")
        return True

    except Exception as e:
        if timer.outcome != "sent":
            timer.outcome = "error"
        timer.reason = type(e).__name__
        db.session.rollback()
        print(f"An unexpected error occurred during buy_token: {str(e)}")
        # Log full traceback for debugging
        import traceback
        traceback.print_exc()
        logger.error(f"An error occurred: {str(e)}")
        return True

# def autosnipe_buy_new_token(token_address: str, user_id: int) -> bool:
def autosnipe_buy_new_token(token_address: str, config: AutoSnipeConfig) -> bool:
    """
    Orchestrates the check and buy process for a newly detected token.
    """
    if not config.active:
        return False

    decision_start = time.monotonic()
    if should_buy_token(token_address, config.user_id, config):
        print(f"Token {token_address} met buy conditions. Attempting to buy...")
        return buy_token(token_address, config, decision_ms=(time.monotonic() - decision_start) * 1000)
        return buy_token(token_address, config.user_id)
    else:
        print(f"Token {token_address} did not meet buy conditions. Skipping buy.")
        return False
    # print(f"\n--- Initiating autosnipe check for token: {token_address} (User: {user_id}) ---")
    # if should_buy_token(token_address, user_id):
    #     print(f"Token {token_address} met buy conditions. Attempting to buy...")
    #     return buy_token(token_address, user_id)
    # else:
    #     print(f"Token {token_address} did not meet buy conditions. Skipping buy.")
    #     return False

# Initialize variables
last_checked_slot = 0  # Start from slot 0
processed_token_mints = set()  # Create an empty set to track processed token mints

def detect_new_tokens_single_pass(last_checked_slot: int, processed_token_mints: set) -> int:
    """
    Legacy HTTP polling detector (fallback when WebSocket is unavailable).
    Performs a single pass of detecting new SPL token mints and triggering autosnipe.
    Returns the current slot after checking.
    """
    from main import app
    with app.app_context():
        try:
            # config = AutoSnipeConfig.query.filter_by(active=True).first()
            configs = AutoSnipeConfig.query.filter_by(active=True).all()
            # user_id = int(config.user_id)
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
            # new_mints_found_in_batch.add("6QaQZFFZkCCqE7UyAFRDBMhvKoui11nv3ugeBvn2pump")  # For testing purposes

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
            # token_address = 'D2QvT2fgdvaLxDLiTFjHeRqeZFXU8UqFdJr7xcgHmoon'
            # # txns = get_token_transactions(token_address, config.min_txns)
            # txns = get_token_specific_transactions(token_address, config.min_txns, config.buy_txns_over_80_usd)
            # print(txns)
            for new_token_address in new_mints_found_in_batch:
                print(f"[Detector] Attempting autosnipe for detected new token: {new_token_address}")
                # autosnipe_buy_new_token(new_token_address, user_id)
                for config in configs:
                    autosnipe_buy_new_token(new_token_address, config)
            return current_slot
        except Exception as e:
            print(f"[Detector] An error occurred in detection pass: {e}")
            return last_checked_slot

autosnipe_trade_bp = Blueprint('autosnipe_trade_bp', __name__)


@autosnipe_trade_bp.route('/api/sniper/stream-status', methods=['GET'])
def sniper_stream_status():
    """Health check for Phase 1 WebSocket sniper ingestion."""
    try:
        from sniper_stream import stream_state, is_ws_active
        return jsonify({
            "status": "ok",
            **stream_state.status(),
            "ws_listener_active": is_ws_active(),
            "use_websocket": SNIPER_USE_WEBSOCKET,
            "http_fallback": SNIPER_HTTP_FALLBACK,
        })
    except ImportError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@autosnipe_trade_bp.route('/api/price/stream-status', methods=['GET'])
def price_stream_status_api():
    """Health check for dashboard live pricing WebSocket."""
    try:
        from price_stream import price_stream_status, is_price_ws_active
        return jsonify({
            "status": "ok",
            **price_stream_status(),
            "ws_listener_active": is_price_ws_active(),
        })
    except ImportError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

#
# @autosnipe_trade_bp.route('/detect_new_tokens', methods=['POST'])
# def detect_new_tokens():
#     global last_checked_slot, processed_token_mints
#     try:
#         # Call the function
#         current_slot = detect_new_tokens_single_pass(last_checked_slot, processed_token_mints)
#
#         # Update last_checked_slot
#         last_checked_slot = current_slot
#
#         return jsonify({
#             "status": "success",
#             "last_checked_slot": last_checked_slot,
#             "processed_token_mints": list(processed_token_mints)
#         })
#     except Exception as e:
#         return jsonify({"status": "error", "message": str(e)}), 500

from apscheduler.schedulers.background import BackgroundScheduler
import atexit

_http_scheduler = None
_ingestion_started = False


def stop_http_fallback_scheduler() -> None:
    """Stop legacy HTTP polling scheduler when WebSocket is active."""
    global _http_scheduler
    if _http_scheduler and _http_scheduler.running:
        _http_scheduler.shutdown(wait=False)
        print("[HTTP Fallback] Stopped — WebSocket ingestion is active.")
    _http_scheduler = None


def _handle_ws_new_token(mint: str) -> None:
    """Called by sniper_stream when a new Pump.fun Create is detected via WebSocket."""
    global processed_token_mints
    from main import app

    if mint in processed_token_mints:
        return
    processed_token_mints.add(mint)

    with app.app_context():
        try:
            from sniper_stream import stream_state
            stream_state.mark_processed(mint)
        except ImportError:
            pass

        configs = AutoSnipeConfig.query.filter_by(active=True).all()
        print(f"[WS Detector] Attempting autosnipe for new token: {mint}")
        for config in configs:
            autosnipe_buy_new_token(mint, config)


def run_detector():
    global last_checked_slot, processed_token_mints
    last_checked_slot = detect_new_tokens_single_pass(last_checked_slot, processed_token_mints)


def _start_http_fallback_scheduler():
    global _http_scheduler
    if _http_scheduler and _http_scheduler.running:
        return
    _http_scheduler = BackgroundScheduler(daemon=True)
    _http_scheduler.add_job(run_detector, "interval", seconds=5)
    _http_scheduler.start()
    atexit.register(lambda: _http_scheduler.shutdown(wait=False))
    print("[HTTP Fallback] Detector scheduler running (every 5s).")


# --- Buy-time entry-price reconciliation ------------------------------------
#
# buy_token() records a *placeholder* initial_price=0.0 (no Geyser, no Shyft,
# no live price of any kind - zero added latency). This fills it in, a few
# seconds later, from the confirmed transaction's own actual token-balance
# change - a fixed historical fact, unlike a live price which keeps moving
# every time you check it, so this is accurate no matter how quickly or
# slowly the job happens to run.


def _actual_tokens_received(tx_data, mint: str, owner_pubkey: str):
    """Our wallet's real token-balance change in a specific confirmed
    transaction (post - pre, for this mint/owner pair)."""
    try:
        meta = tx_data.transaction.meta
        post_balances = meta.post_token_balances or []
        pre_balances = meta.pre_token_balances or []
    except AttributeError:
        return None

    mint_pk = Pubkey.from_string(mint)
    owner_pk = Pubkey.from_string(owner_pubkey)

    post_bal = next((b for b in post_balances if b.mint == mint_pk and b.owner == owner_pk), None)
    if post_bal is None:
        return None
    post_amount = post_bal.ui_token_amount.ui_amount or 0.0

    pre_bal = next((b for b in pre_balances if b.mint == mint_pk and b.owner == owner_pk), None)
    pre_amount = (pre_bal.ui_token_amount.ui_amount or 0.0) if pre_bal else 0.0

    received = post_amount - pre_amount
    return received if received > 0 else None


def _apply_buy_price_reconciliation(trade, sol_spent: float, actual_tokens: float) -> None:
    """
    Sets Trade.initial_price (and purchased_token_amount/
    initial_token_amount, if nothing has sold from the position yet) from
    (sol_spent * SOL/USD rate) / actual_tokens - sol_spent and actual_tokens
    both come from real execution data (config + confirmed transaction);
    only the SOL/USD rate is a fresh read, and that figure is comparatively
    stable second to second (unlike a specific token's own price), so it
    isn't the source of inaccuracy a correction needs to guard against.
    """
    if trade.executed:
        return  # position already closed; nothing left to correct

    sol_usd_rate = get_sol_usd_rate()
    if sol_usd_rate and sol_usd_rate > 0 and actual_tokens > 0:
        trade.initial_price = (sol_spent * sol_usd_rate) / actual_tokens
    else:
        logger.warning(
            f"[BuyReconcile] SOL/USD rate unavailable for trade {trade.id}; "
            "leaving initial_price at its placeholder value."
        )

    if not trade.tp_pct_sold:
        # No partial sell has touched this position yet - safe to correct
        # the recorded quantity too. If a partial sell already happened
        # (against the old quote-based estimate), leave quantities alone -
        # quantity bookkeeping belongs to the sell flow once it has started
        # acting on a trade; only the price is corrected in that case.
        trade.purchased_token_amount = actual_tokens
        trade.initial_token_amount = actual_tokens


def reconcile_pending_buys(app) -> None:
    """
    Resolve every "sent" buy to its actual on-chain outcome and compute the
    real entry price/quantity from what the confirmed transaction actually
    delivered. Mirrors the sell flow's own reconcile_pending_sells() (same
    TradeHistory.status states, same bounded timeout), but fills in pricing
    rather than restoring a position - a buy transaction that failed/never
    confirmed is left with its placeholder price rather than being closed
    out, since reversing a buy's on-chain effects is outside the scope of a
    pricing fix.

    Runs as its own periodic job, separate from buy execution, so this
    never adds latency to a snipe buy.
    """
    with app.app_context():
        try:
            pending = TradeHistory.query.filter_by(status="sent", trade_type="BUY").all()
        except Exception as e:
            logger.warning(f"[BuyReconcile] could not load pending buys: {e}")
            return
        if not pending:
            return

        for history in pending:
            age_sec = (
                (datetime.utcnow() - history.timestamp).total_seconds() if history.timestamp else 0.0
            )
            try:
                # search_transaction_history=True: without it, the RPC node
                # only answers from its short-lived recent-status cache and
                # returns None for anything that's aged out of it (a couple
                # of minutes), even when the transaction actually finalized
                # - which would otherwise eventually mislabel a successful
                # buy as failed_timeout.
                resp = solana_client.get_signature_statuses(
                    [Signature.from_string(history.tx_id)], search_transaction_history=True
                )
                status = (resp.value or [None])[0]
            except Exception as e:
                logger.warning(f"[BuyReconcile] get_signature_statuses failed for {history.tx_id}: {e}")
                continue

            if status is None:
                if age_sec >= SNIPER_BUY_RECONCILE_TIMEOUT_SEC:
                    history.status = "failed_timeout"
                    db.session.commit()
                    logger.warning(
                        f"[BuyReconcile] buy tx {history.tx_id} for trade {history.trade_id} "
                        f"never confirmed within {SNIPER_BUY_RECONCILE_TIMEOUT_SEC:.0f}s; "
                        "leaving the placeholder entry price in place."
                    )
                continue

            if status.err is not None:
                history.status = "failed"
                db.session.commit()
                logger.warning(
                    f"[BuyReconcile] buy tx {history.tx_id} for trade {history.trade_id} "
                    "failed on-chain; leaving the placeholder entry price in place "
                    "(this job only fills in pricing, it does not close/restore the position)."
                )
                continue

            if status.confirmation_status not in (
                TransactionConfirmationStatus.Confirmed,
                TransactionConfirmationStatus.Finalized,
            ):
                if age_sec >= SNIPER_BUY_RECONCILE_TIMEOUT_SEC:
                    history.status = "failed_timeout"
                    db.session.commit()
                continue

            trade = Trade.query.get(history.trade_id) if history.trade_id else None
            if trade is None:
                history.status = "confirmed"
                db.session.commit()
                continue

            try:
                tx_response = solana_client.get_transaction(
                    Signature.from_string(history.tx_id), encoding='jsonParsed',
                    max_supported_transaction_version=0,
                )
                tx_data = tx_response.value
            except Exception as e:
                logger.warning(f"[BuyReconcile] get_transaction failed for {history.tx_id}: {e}")
                continue
            if not tx_data:
                continue

            actual_tokens = _actual_tokens_received(tx_data, trade.token_address, trade.to_pubkey)
            if actual_tokens is None:
                # get_signature_statuses reporting "confirmed" can transiently
                # race ahead of get_transaction actually returning complete
                # balance data for that same signature - retry on later
                # cycles rather than giving up on the first miss, exactly
                # like the "not yet Confirmed/Finalized" case above. Only
                # give up once the bounded timeout is reached.
                if age_sec >= SNIPER_BUY_RECONCILE_TIMEOUT_SEC:
                    logger.warning(
                        f"[BuyReconcile] could not parse actual tokens received for trade {trade.id} "
                        f"(tx {history.tx_id}) within {SNIPER_BUY_RECONCILE_TIMEOUT_SEC:.0f}s; "
                        "leaving the placeholder entry price in place."
                    )
                    history.status = "confirmed"
                    db.session.commit()
                continue

            try:
                db.session.refresh(trade)
            except Exception as e:
                logger.warning(f"[BuyReconcile] could not refresh trade {trade.id}: {e}")
                continue

            _apply_buy_price_reconciliation(trade, history.amount, actual_tokens)
            history.status = "confirmed"
            history.execution_price = trade.initial_price
            try:
                db.session.commit()
            except Exception as e:
                logger.error(f"[BuyReconcile] failed to save corrected price for trade {trade.id}: {e}")
                db.session.rollback()
                continue

            logger.info(
                f"[BuyReconcile] trade {trade.id} ({trade.token_address}): entry price set to "
                f"{trade.initial_price} using actual {actual_tokens} tokens received "
                f"(tx {history.tx_id})."
            )


_buy_reconcile_scheduler = None


def start_buy_reconciliation_scheduler() -> None:
    """Starts the buy-time entry-price reconciliation job. Runs
    unconditionally once Sniper ingestion starts, independent of whether
    detection is on the WebSocket or HTTP-fallback path."""
    global _buy_reconcile_scheduler
    if _buy_reconcile_scheduler is not None:
        return
    from main import app

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        lambda: reconcile_pending_buys(app), "interval", seconds=SNIPER_BUY_RECONCILE_INTERVAL_SEC
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    _buy_reconcile_scheduler = scheduler
    print(f"[BuyReconcile] scheduler running (every {SNIPER_BUY_RECONCILE_INTERVAL_SEC:.0f}s).")


def start_sniper_ingestion():
    """
    Start WebSocket ingestion (Phase 1) or HTTP polling fallback.
    Call once from main.py (WERKZEUG child process only) to avoid duplicate schedulers.
    """
    global _ingestion_started
    if _ingestion_started:
        return
    _ingestion_started = True

    start_buy_reconciliation_scheduler()

    if SNIPER_USE_WEBSOCKET:
        try:
            from sniper_stream import start_sniper_stream, wait_for_connection

            def _on_ws_connected():
                stop_http_fallback_scheduler()

            if start_sniper_stream(_handle_ws_new_token, on_connected=_on_ws_connected):
                if wait_for_connection(timeout_sec=30):
                    print(
                        "[WS] Sniper WebSocket connected and listening for Pump.fun events."
                    )
                    stop_http_fallback_scheduler()
                else:
                    from settings import get_solana_ws_urls
                    print(
                        "[WS] Connecting... endpoints: "
                        + ", ".join(get_solana_ws_urls())
                    )
                return
        except Exception as exc:
            print(f"[WS] Failed to start WebSocket sniper: {exc}")

    if SNIPER_HTTP_FALLBACK:
        print("[Sniper] WebSocket disabled or failed — using HTTP polling fallback.")
        _start_http_fallback_scheduler()
    else:
        print("[Sniper] No ingestion started. Enable SNIPER_USE_WEBSOCKET or SNIPER_HTTP_FALLBACK.")
