import logging
import os
import atexit
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from dotenv import load_dotenv
from models import db, Wallet, Trade, TradeLog, TradeHistory
import requests
import base64
from flask import Blueprint
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from settings import (
    solana_client,
    SNIPER_SELL_PRICE_WORKERS,
    SNIPER_SELL_STREAM,
    SNIPER_SELL_STREAM_SYNC_SEC,
    SNIPER_SELL_EVENT_WORKERS,
    SNIPER_SELL_EMPTY_COOLDOWN_SEC,
    SNIPER_SELL_EMPTY_GRACE_SEC,
    SNIPER_SELL_RETRY_COOLDOWN_SEC,
    SNIPER_SELL_RECONCILE_INTERVAL_SEC,
    SNIPER_SELL_RECONCILE_TIMEOUT_SEC,
    SNIPER_SELL_CYCLE_INTERVAL_SEC,
    SNIPER_SELL_YELLOWSTONE_COOLDOWN_SEC,
)
from latency_log import StageTimer
from sell_stream import SellStream
from shyft_pricing import get_token_price
from yellowstone_pricing import YellowstonePricingError, get_yellowstone_price, is_yellowstone_enabled
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

# Reused across every quote/swap call instead of a bare requests.get/post,
# which opens a brand-new TCP+TLS connection per call. Keep-alive here saves
# a full handshake on every sell's swap request right after its quote request.
_http_session = requests.Session()

price_tracking = {}


# mint -> time.monotonic() before which get_sniper_sell_price skips
# Yellowstone entirely and goes straight to Shyft. See
# SNIPER_SELL_YELLOWSTONE_COOLDOWN_SEC.
_yellowstone_cooldown: dict = {}


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

    A mint that just timed out goes on a short cooldown instead of
    re-attempting Yellowstone on the very next tick: against a real set of
    ~30 open (mostly quiet) positions, re-attempting on every tick made a
    full price pass take 20-30s (10 workers x an 8s timeout each, repeatedly)
    and exhausted the account's Yellowstone connection limit. Shyft's RPC
    read is a live point-in-time read regardless, so skipping Yellowstone
    during the cooldown costs no price freshness.
    """
    if is_yellowstone_enabled():
        if time.monotonic() < _yellowstone_cooldown.get(token_address, 0.0):
            return get_token_price(token_address, include_token_details=False)
        try:
            price_data = get_yellowstone_price(token_address)
            _yellowstone_cooldown.pop(token_address, None)
            print(
                "[Sell Price][Yellowstone/Geyser] Successfully retrieved "
                f"token={token_address} usd_price={price_data.get('usdPrice')} "
                f"source={price_data.get('source', 'Yellowstone/Geyser')}",
                flush=True,
            )
            return price_data
        except YellowstonePricingError as exc:
            _yellowstone_cooldown[token_address] = time.monotonic() + SNIPER_SELL_YELLOWSTONE_COOLDOWN_SEC
            logger.warning(
                f"Yellowstone price fetch failed for {token_address} "
                f"({exc}); falling back to Shyft RPC snapshot and skipping "
                f"Yellowstone for this mint for {SNIPER_SELL_YELLOWSTONE_COOLDOWN_SEC:.0f}s."
            )
            return get_token_price(token_address, include_token_details=False)
    return get_token_price(token_address, include_token_details=False)


ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def get_wallet_token_balance(owner: Pubkey, mint: str) -> tuple[int, int | None]:
    """
    Raw amount and decimals held in the wallet's associated token account for mint.

    Uses plain account reads only: getTokenAccountsByOwner needs secondary
    indexes, and the local validator excludes Token-2022 from them.

    Reads at confirmed commitment: the client default (finalized) is ~30
    slots behind, so a token bought seconds ago read as a 0 balance there.
    """
    mint_pk = Pubkey.from_string(mint)
    mint_account = solana_client.get_account_info(mint_pk, commitment=Confirmed).value
    if mint_account is None:
        raise ValueError(f"mint account {mint} not found")
    ata, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(mint_account.owner), bytes(mint_pk)], ASSOCIATED_TOKEN_PROGRAM_ID
    )
    if solana_client.get_account_info(ata, commitment=Confirmed).value is None:
        return 0, None
    balance = solana_client.get_token_account_balance(ata, commitment=Confirmed).value
    return int(balance.amount), balance.decimals


# Preflight sells against the confirmed bank (see get_wallet_token_balance).
SELL_TX_OPTS = TxOpts(preflight_commitment=Confirmed)


_trade_locks: dict = {}
_trade_locks_guard = threading.Lock()
# trade id -> time.monotonic() before which a sell attempt is not retried
_retry_after: dict = {}
# trade id -> time.monotonic() its wallet balance was first read as 0
_empty_since: dict = {}
_UNROUTABLE_QUOTE_ERRORS = {"TOKEN_NOT_TRADABLE", "NO_ROUTES_FOUND"}


def _trade_lock(trade_id) -> threading.Lock:
    with _trade_locks_guard:
        return _trade_locks.setdefault(trade_id, threading.Lock())


def process_trade_sell(trade_data, current_price, trigger: str = "cycle", timings: dict | None = None) -> None:
    """
    Evaluate one trade at current_price and execute the sell if a rule fires.

    Shared by the periodic sell cycle and the instant (trade-activity) path;
    must run inside an app context. The per-trade lock plus a fresh DB read
    guarantee the two paths never sell the same trade at once or on stale
    state (e.g. a cycle that loaded the trade before an instant sale).

    timings: stages already measured by the caller (queue wait, price fetch),
    included in the "[latency]" line logged when a sell rule fires.
    """
    lock = _trade_lock(trade_data.id)
    if not lock.acquire(blocking=False):
        return  # the other path is handling this trade right now
    timer = StageTimer("sell", trade=trade_data.id, mint=trade_data.token_address, trigger=trigger)
    for stage, ms in (timings or {}).items():
        timer.add(stage, ms)
    outcome = None
    try:
        try:
            with timer.stage("db_refresh"):
                db.session.refresh(trade_data)
        except Exception as e:
            logger.warning(f"Could not refresh trade {trade_data.id}: {e}")
            return
        if trade_data.executed:
            return
        outcome = _sell_if_due(trade_data, current_price, trigger, timer)
        if outcome == "empty":
            _retry_after[trade_data.id] = time.monotonic() + SNIPER_SELL_EMPTY_COOLDOWN_SEC
            timer.fields["cooldown_sec"] = SNIPER_SELL_EMPTY_COOLDOWN_SEC
        elif outcome == "failed":
            _retry_after[trade_data.id] = time.monotonic() + SNIPER_SELL_RETRY_COOLDOWN_SEC
            timer.fields["cooldown_sec"] = SNIPER_SELL_RETRY_COOLDOWN_SEC
        elif outcome == "sold":
            _retry_after.pop(trade_data.id, None)
    finally:
        lock.release()
        # Only attempts where a sell rule fired: price checks without a
        # decision run on every trade of the token and would flood the log.
        if outcome not in (None, "cooling"):
            timer.finish(outcome)


def _sell_if_due(trade_data, current_price, trigger: str, timer: StageTimer | None = None):
    """
    Returns None when no sell rule fires, "cooling" while a recent failed
    attempt is still in its cooldown, "sold" after a sent transaction,
    "empty" when the wallet has nothing to sell (or the wallet/key can't be
    used), and "failed" for errors worth retrying soon. timer.reason says why.
    """
    timer = timer or StageTimer("sell", trade=trade_data.id)
    if not current_price or current_price <= 0:
        return
    initial_price = trade_data.initial_price
    if initial_price <= 0:
        logger.warning(f"Invalid initial price for trade {trade_data.id}. Skipping auto-snipe.")
        return

    decision = evaluate_autosnipe_sell(trade_data, current_price, price_tracking)
    amount_to_trade, message = decision.amount, decision.message
    if amount_to_trade <= 0:
        return
    if time.monotonic() < _retry_after.get(trade_data.id, 0):
        return "cooling"
    trade_ref = f"trade {trade_data.id} ({trade_data.token_address})"
    logger.info(
        f"[Sell decision][{trigger}] {trade_ref}: {message} | "
        f"price={current_price} x{current_price / initial_price:.3f} amount={amount_to_trade}"
    )
    try:
        # Continue with the same logic for performing the trade
        wallet = Wallet.query.filter_by(public_key=trade_data.to_pubkey).first()
        if not wallet:
            logger.warning(f"Wallet not found for {trade_data.to_pubkey}, skipping {trade_ref}.")
            timer.reason = "wallet_not_found"
            return "empty"
        private_key_path = wallet.private_key
        if not os.path.exists(private_key_path):
            logger.error(f"Private key file missing for wallet {wallet.public_key}, skipping {trade_ref}.")
            timer.reason = "key_file_missing"
            return "empty"

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
            timer.reason = "invalid_key"
            return "empty"

        try:
            with timer.stage("balance"):
                raw_balance, decimals = get_wallet_token_balance(
                    wallet_keypair.pubkey(), trade_data.token_address
                )
        except Exception as e:
            logger.warning(f"Could not read wallet token balance for {trade_ref}: {e}")
            timer.reason = "balance_read_failed"
            return "failed"
        if raw_balance <= 0:
            # A sell rule can fire before the buy has landed (e.g. a stop
            # loss seconds after the buy). Retry on the short cooldown for a
            # grace period before treating the wallet as genuinely empty.
            first_empty = _empty_since.setdefault(trade_data.id, time.monotonic())
            if time.monotonic() - first_empty < SNIPER_SELL_EMPTY_GRACE_SEC:
                logger.warning(
                    f"Wallet holds 0 tokens for {trade_ref} yet; the buy may not have landed, retrying shortly."
                )
                timer.reason = "zero_balance_in_grace"
                return "failed"
            logger.warning(
                f"Wallet holds 0 tokens for {trade_ref} "
                f"(DB still shows {trade_data.purchased_token_amount}); skipping sell."
            )
            timer.reason = "zero_balance"
            return "empty"
        _empty_since.pop(trade_data.id, None)

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
        with timer.stage("quote"):
            quote_response = _http_session.get(quote_endpoint, params=quote_params, headers=headers, timeout=15)
        if quote_response.status_code != 200:
            quote_error = quote_response.json()
            logger.error(f"Error fetching quote for {trade_ref}: {quote_error}")
            # Jupiter won't route this token at all right now; retrying every
            # few seconds only spams the API and the logs.
            if isinstance(quote_error, dict) and quote_error.get("errorCode") in _UNROUTABLE_QUOTE_ERRORS:
                timer.reason = f"quote_{quote_error.get('errorCode')}"
                return "empty"
            timer.reason = f"quote_http_{quote_response.status_code}"
            return "failed"

        quote_data = quote_response.json()
        logger.info(f"Quote data: {quote_data}")

        swap_request = {
            "userPublicKey": str(wallet_keypair.pubkey()),
            "quoteResponse": quote_data,
            "computeUnitPriceMicroLamports": int(trade_data.default_gas_fee * 1_000_000),
            "wrapUnwrapSOL": True
        }

        swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
        with timer.stage("swap"):
            swap_response = _http_session.post(swap_endpoint, json=swap_request, headers=headers, timeout=15)
        if swap_response.status_code != 200:
            logger.error(f"Error performing swap for {trade_ref}: {swap_response.json()}")
            timer.reason = f"swap_http_{swap_response.status_code}"
            return "failed"

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
            with timer.stage("send"):
                rpc_response = solana_client.send_transaction(signed_transaction, opts=SELL_TX_OPTS)
        except Exception as e:
            logger.error(f"Error sending transaction for {trade_ref}: {str(e)}")
            timer.reason = "send_rejected"
            return "failed"
        signature = str(rpc_response.value)
        timer.fields["sig"] = signature
        logger.info(f"{message}View transaction on Solscan: https://solscan.io/tx/{signature}")
        logger.info(f"Transaction sent successfully! Signature: {signature}")
        print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")

        db_start = time.monotonic()
        try:
            executed_trade = TradeHistory(
                trade_id=trade_data.id,
                token_address=trade_data.token_address,
                trade_type="SELL",
                trade_kind="AutoSnipe",
                amount=sold_tokens,
                execution_price=current_price if current_price else 0,
                tx_id=signature,
                # Submitted, not yet confirmed on-chain; reconcile_pending_sells
                # resolves this to "confirmed"/"failed" shortly after.
                status="sent",
            )
            db.session.add(executed_trade)
            db.session.commit()
            if decision.take_profit_pct is not None:
                if trade_data.initial_token_amount is None:
                    trade_data.initial_token_amount = trade_data.purchased_token_amount
                trade_data.tp_pct_sold = decision.take_profit_pct
                trade_data.last_tp_tier = decision.take_profit_tier
            if full_exit:
                trade_data.purchased_token_amount = 0
            else:
                trade_data.purchased_token_amount -= amount_to_trade
            if trade_data.purchased_token_amount <= 0:
                trade_data.executed = True
            db.session.commit()
        except Exception as e:
            # The sale was sent; retrying soon on the stale row could sell twice.
            logger.error(f"Sale sent for {trade_ref} (tx {signature}) but DB update failed: {e}")
            db.session.rollback()
            timer.reason = "db_update_failed_after_send"
            return "empty"
        timer.add("db", (time.monotonic() - db_start) * 1000)
        return "sold"

    except Exception as e:
        logger.error(f"Auto-snipe sell error for {trade_ref}: {str(e)}")
        db.session.rollback()
        timer.reason = type(e).__name__
        return "failed"


# --- Instant path: re-check a trade the moment its token trades -------------

_instant_pool = ThreadPoolExecutor(max_workers=max(1, SNIPER_SELL_EVENT_WORKERS), thread_name_prefix="sell-instant")
_instant_guard = threading.Lock()
_instant_running: set = set()
# mint -> time.monotonic() of the activity its pending re-check answers
_instant_rerun: dict = {}


def on_token_activity(app, mint: str) -> None:
    """
    Called by the sell stream for every trade on mint. Bursts are coalesced:
    one check runs per mint at a time, and activity during a check triggers
    exactly one more check afterwards, so the latest price is always seen.
    """
    with _instant_guard:
        if mint in _instant_running:
            _instant_rerun.setdefault(mint, time.monotonic())
            return
        _instant_running.add(mint)
    _instant_pool.submit(_run_instant_checks, app, mint, time.monotonic())


def _run_instant_checks(app, mint: str, requested_at: float) -> None:
    while True:
        check_trades_for_mint(app, mint, requested_at)
        with _instant_guard:
            if mint in _instant_rerun:
                requested_at = _instant_rerun.pop(mint)
                continue
            _instant_running.discard(mint)
            return


def check_trades_for_mint(app, mint: str, requested_at: float | None = None) -> None:
    # Time from the token's trade notification until this check started.
    queue_wait = (time.monotonic() - requested_at) * 1000 if requested_at else None
    with app.app_context():
        try:
            trades = Trade.query.filter_by(token_address=mint, executed=False, auto_snipe=True).all()
            if not trades:
                return
            # A trade just landed, so read the current on-chain state directly
            # (Yellowstone's one-shot subscribe would wait for the *next* trade).
            price_start = time.monotonic()
            price = get_token_price(mint, include_token_details=False)["usdPrice"]
            timings = {"price": (time.monotonic() - price_start) * 1000}
            if queue_wait is not None:
                timings = {"queue_wait": queue_wait, **timings}
            for trade_data in trades:
                process_trade_sell(trade_data, price, trigger="instant", timings=timings)
        except Exception as e:
            logger.warning(f"[Instant] check failed for {mint}: {e}")
            db.session.rollback()


def _open_trade_mints(app) -> set:
    with app.app_context():
        rows = Trade.query.with_entities(Trade.token_address).filter_by(executed=False, auto_snipe=True).all()
        return {row[0] for row in rows}


# --- Reconciliation: confirm "sent" sells, undo the ones that failed --------


def _restore_unsold_position(trade, history, status: str) -> None:
    """
    A sell tx that never confirmed; give the tokens it claimed to sell back
    to the open position so the bot offers them for sale again.

    Runs under the trade's lock so it can't race a live sell attempt. Does
    not restore take-profit tier bookkeeping (tp_pct_sold/last_tp_tier): the
    position size is made whole either way, this only means that specific
    tier's share may not be re-offered at that exact tier again and instead
    waits for the next tier or a stop-loss/trailing-stop rule.
    """
    lock = _trade_lock(trade.id)
    with lock:
        try:
            db.session.refresh(trade)
        except Exception as e:
            logger.warning(f"[Reconcile] could not refresh trade {trade.id}: {e}")
            return
        trade.purchased_token_amount = (trade.purchased_token_amount or 0) + history.amount
        trade.executed = False
        history.status = status
        try:
            db.session.commit()
        except Exception as e:
            logger.error(f"[Reconcile] failed to restore trade {trade.id} after {status} sell: {e}")
            db.session.rollback()
            return
        _retry_after.pop(trade.id, None)
        logger.error(
            f"[Reconcile] sell tx {history.tx_id} for trade {trade.id} ({trade.token_address}) "
            f"{status} on-chain; restored {history.amount} tokens to the open position."
        )


def reconcile_pending_sells(app) -> None:
    """
    Resolve every "sent" sell to its actual on-chain outcome.

    send_transaction succeeding only means the RPC node accepted the
    transaction for broadcast, not that it landed (expired blockhash,
    slippage/liquidity moved before it processed, node never forwarded it,
    etc.). Left unchecked, a failed send still looks like a completed sale
    forever: purchased_token_amount was already reduced and the trade may be
    marked executed, so no rule ever reconsiders tokens the wallet still
    holds. This runs as its own periodic job (separate from the sell-decision
    cycle) so a slow RPC here never delays detecting the next sell condition.
    """
    with app.app_context():
        try:
            pending = TradeHistory.query.filter_by(status="sent").all()
        except Exception as e:
            logger.warning(f"[Reconcile] could not load pending sells: {e}")
            return
        if not pending:
            return

        for history in pending:
            age_sec = (
                (datetime.utcnow() - history.timestamp).total_seconds() if history.timestamp else 0.0
            )
            try:
                resp = solana_client.get_signature_statuses([Signature.from_string(history.tx_id)])
                status = (resp.value or [None])[0]
            except Exception as e:
                logger.warning(f"[Reconcile] get_signature_statuses failed for {history.tx_id}: {e}")
                continue

            trade = Trade.query.get(history.trade_id) if history.trade_id else None

            if status is None:
                # Never seen by the node (dropped) or not old enough to give up on yet.
                if age_sec >= SNIPER_SELL_RECONCILE_TIMEOUT_SEC:
                    if trade is not None:
                        _restore_unsold_position(trade, history, "failed_timeout")
                    else:
                        history.status = "failed_timeout"
                        db.session.commit()
                continue

            if status.err is not None:
                if trade is not None:
                    _restore_unsold_position(trade, history, "failed")
                else:
                    history.status = "failed"
                    db.session.commit()
                continue

            if status.confirmation_status in (
                TransactionConfirmationStatus.Confirmed,
                TransactionConfirmationStatus.Finalized,
            ):
                history.status = "confirmed"
                db.session.commit()
                logger.info(
                    f"[latency] path=sell_confirm trade={history.trade_id} mint={history.token_address} "
                    f"sig={history.tx_id} outcome=confirmed total={age_sec * 1000:.1f}"
                )
            elif age_sec >= SNIPER_SELL_RECONCILE_TIMEOUT_SEC:
                # Processed but never reached "confirmed" within the timeout.
                if trade is not None:
                    _restore_unsold_position(trade, history, "failed_timeout")
                else:
                    history.status = "failed_timeout"
                    db.session.commit()


def auto_snipe_auto_sell_schedular(app):
    # Auto-snipe logic to sell tokens based on configurable conditions
    def auto_snipe_sell():
        with app.app_context():
            try:
                """Handles the auto-snipe logic for selling tokens based on trade settings."""
                # Fetch trades that have not been executed
                trades = Trade.query.filter_by(executed=False, auto_snipe=True).order_by(Trade.id.desc()).all()

                def _fetch_price(mint):
                    # Own app context: the module logger's DB handler then gets
                    # a per-thread session instead of sharing this thread's.
                    with app.app_context():
                        start = time.monotonic()
                        price_data = get_sniper_sell_price(mint)
                        done = time.monotonic()
                        return price_data, (done - start) * 1000, done

                # Prices are fetched in parallel (a dead token can take the full
                # Yellowstone timeout); each trade is evaluated and sold on this
                # thread as soon as its price arrives.
                price_pool = ThreadPoolExecutor(
                    max_workers=max(1, SNIPER_SELL_PRICE_WORKERS), thread_name_prefix="sell-price"
                )
                price_futures = {price_pool.submit(_fetch_price, t.token_address): t for t in trades}
                price_pool.shutdown(wait=False)
                for price_future in as_completed(price_futures):
                    trade_data = price_futures[price_future]
                    # A failed/invalid Yellowstone fetch skips this trade for the
                    # current cycle rather than selling on a stale or guessed price.
                    try:
                        current_price_, price_ms, priced_at = price_future.result()
                        current_price = current_price_['usdPrice']
                    except YellowstonePricingError as e:
                        logger.warning(f"Yellowstone price fetch failed for trade {trade_data.id}: {e}")
                        continue
                    except Exception as e:
                        logger.warning(f"Failed to fetch price for trade {trade_data.id}: {e}")
                        continue
                    # queue_wait: priced but waiting for earlier sells in this cycle.
                    process_trade_sell(trade_data, current_price, timings={
                        "price": price_ms, "queue_wait": (time.monotonic() - priced_at) * 1000,
                    })
                print("autosnipe token sell successfully executed")
                return None
            except Exception as e:
                db.session.rollback()
                logger.error(f"Unexpected error in auto_trade loop: {e}")
                time.sleep(1)

    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_snipe_sell, 'interval', seconds=SNIPER_SELL_CYCLE_INTERVAL_SEC)
    scheduler.add_job(
        lambda: reconcile_pending_sells(app), 'interval', seconds=SNIPER_SELL_RECONCILE_INTERVAL_SEC
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

    if SNIPER_SELL_STREAM:
        stream = SellStream(
            get_open_mints=lambda: _open_trade_mints(app),
            on_activity=lambda mint: on_token_activity(app, mint),
            sync_interval=SNIPER_SELL_STREAM_SYNC_SEC,
        )
        stream.start()
        atexit.register(stream.stop)
