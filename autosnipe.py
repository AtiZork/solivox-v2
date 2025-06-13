from flask_jwt_extended import jwt_required, get_jwt_identity
from autosnipe_logic import (
    set_listener_running_status,
    set_processor_running_status,
    solana_logs_listener,
    auto_buy_processor,
    listener_running # Ensure this is used for UI status
)
from models import db, AutoSnipeConfig, User, TradeLog
import threading
import os
import asyncio
import logging
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, Blueprint, current_app
from datetime import datetime

# --- Initialize global thread variables here ---
listener_thread = None
auto_buy_thread = None

autosnipe_bp = Blueprint('autosnipe_bp', __name__)


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
@autosnipe_bp.route('/api/autosnipe', methods=['POST'])
@jwt_required()
def autosnipe():
    try:
        user_id = get_jwt_identity()
        data = request.get_json()
        # config = AutoSnipeConfig.query.first()
        config = AutoSnipeConfig.query.filter_by(user_id=user_id).first()
        if not config:
            config = AutoSnipeConfig()
        active = data.get("active", True)  # default True if missing
        if isinstance(active, str):
            active = active.lower() == "true"

        config.active = bool(active)
        # Direct assign assuming correct types from frontend
        config.buy_txns_over_80_usd = data.get('buy_txns_over_80_usd', 5)
        config.min_txns = data['min_txns']
        config.launch_delay = data['launch_delay']
        config.buy_amount = data['buy_amount']
        config.slippage = data['slippage']
        config.priority_fee = data['priority_fee']
        config.drop_cutoff = data.get('drop_cutoff', config.drop_cutoff)
        config.drop_until_profit = data.get('drop_until_profit', config.drop_until_profit)
        config.drop_after_100 = data.get('drop_after_100', config.drop_after_100)
        config.drop_after_400 = data.get('drop_after_400', config.drop_after_400)
        config.sell_at_200 = data.get('sell_at_200', config.sell_at_200)
        config.sell_at_400 = data.get('sell_at_400', config.sell_at_400)
        config.sell_at_1000 = data.get('sell_at_1000', config.sell_at_1000)
        config.sell_at_1500 = data.get('sell_at_1500', config.sell_at_1500)
        config.sell_at_2500 = data.get('sell_at_2500', config.sell_at_2500)
        config.sell_at_4000 = data.get('sell_at_4000', config.sell_at_4000)
        config.sell_at_10000 = data.get('sell_at_10000', config.sell_at_10000)
        config.user_id = int(user_id)

        config.timestamp = datetime.utcnow()

        db.session.add(config)
        db.session.commit()

        return jsonify({'message': 'AutoSnipe config saved successfully'}), 200
    except Exception as e:
        db.session.rollback()
        print(f"Error saving AutoSnipe config: {e}")
        return jsonify({'error': 'Failed to save AutoSnipe config'}), 500
# listing api
@autosnipe_bp.route('/api/autosnipe', methods=['GET'])
@jwt_required()
def get_autosnipe():
    # Get the user identity from the JWT
    user_id = get_jwt_identity()

    # Optional: You can fetch the user from the database (if needed)
    user = User.query.get(user_id)
    if not user:
        return jsonify({"msg": "User not found"}), 404
    # config = AutoSnipeConfig.query.first()
    config = AutoSnipeConfig.query.filter_by(user_id=user_id).first()
    if config:
        return jsonify({
            "active": config.active,
            "buy_txns_over_80_usd": config.buy_txns_over_80_usd,
            "min_txns": config.min_txns,
            "launch_delay": config.launch_delay,
            "buy_amount": config.buy_amount,
            "slippage": config.slippage,
            "priority_fee": config.priority_fee,
            "drop_cutoff": config.drop_cutoff,
            "drop_until_profit": config.drop_until_profit,
            "drop_after_100": config.drop_after_100,
            "drop_after_400": config.drop_after_400,
            "sell_at_200": config.sell_at_200,
            "sell_at_400": config.sell_at_400,
            "sell_at_1000": config.sell_at_1000,
            "sell_at_1500": config.sell_at_1500,
            "sell_at_2500": config.sell_at_2500,
            "sell_at_4000": config.sell_at_4000,
            "sell_at_10000": config.sell_at_10000,
        })
    return jsonify({})


@autosnipe_bp.route('/start_all', methods=['POST'])
def start_all_bots():
    """Starts both the Solana listener and the auto-buy processor."""
    global listener_thread, auto_buy_thread

    # 1. Starting the Solana Listener Thread
    if not listener_thread or not listener_thread.is_alive():
        set_listener_running_status(True)
        app_instance = current_app._get_current_object()  # get actual app instance

        listener_thread = threading.Thread(target=_run_async_listener_with_context, args=(app_instance,), daemon=True)
        listener_thread.start()
        logger.info("Solana listener thread initiated.")
    else:
        logger.info("Solana listener is already running.")

    # 2. Starting the Auto-Buy Processor Thread
    if not auto_buy_thread or not auto_buy_thread.is_alive():
        # Create a new Thread object for the auto-buy processor
        # `target=_run_auto_buy_processor` specifies the function to run.
        # `args=(app,)` passes the Flask `app` instance to the target function.
        # This is CRUCIAL because `auto_buy_processor` needs the `app` context to interact with the database (`db`).
        # auto_buy_thread = threading.Thread(target=_run_auto_buy_processor, args=(app,), daemon=True)
        auto_buy_thread = threading.Thread(target=_run_auto_buy_processor, args=(current_app._get_current_object(),), daemon=True)

        auto_buy_thread.start() # Start the thread, which calls `_run_auto_buy_processor`
        logger.info("Auto-buy processor thread initiated.")
        return jsonify({"status": "success", "message": "Sniper bot for autobuy token started successfully"}), 200

    else:
        logger.info("Auto-buy processor is already running.")
        return jsonify({"status": "success", "message": "Auto-buy processor is already running"}), 200

@autosnipe_bp.route('/stop_all', methods=['POST'])
def stop_all_bots():
    """Stops both the Solana listener and the auto-buy processor."""
    global listener_thread, auto_buy_thread
    logger.info("Stopping all bots...")

    # Signal the listener thread to stop
    # This changes the `listener_running` flag to False, causing the `while` loop
    # in `solana_logs_listener` to eventually terminate.
    set_listener_running_status(False)
    logger.info("Stop signal sent to listener.")

    # Signal the processor thread to stop
    # This changes the `processor_running` flag to False, causing the `while` loop
    # in `auto_buy_processor` to eventually terminate.
    set_processor_running_status(False)
    logger.info("Stop signal sen to processor.")

    return jsonify({"status": "success", "message": "Sniper bot stop"}), 200

# --- Helper functions to run async listener and auto-buyer in threads ---

def _run_async_listener():
    """Helper to run the async listener in a separate event loop."""
    # Each thread needs its own asyncio event loop for async operations.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Run the async listener function until it completes (when `listener_running` becomes False)
        loop.run_until_complete(solana_logs_listener())
    except asyncio.CancelledError:
        logger.info("Listener's asyncio loop was cancelled.")
    finally:
        loop.close() # Close the event loop when done
        logger.info("Listener's asyncio loop closed.")


def _run_auto_buy_processor(app_instance):
    """Helper to run the auto-buy processor within the Flask app context."""
    # The `auto_buy_processor` function needs to interact with the SQLAlchemy database.
    # SQLAlchemy requires an "application context" to know which Flask app and database
    # session it's associated with. We create this context for the thread.
    with app_instance.app_context():
        # Call the auto-buy processor function. It contains its own `while` loop
        # that continues as long as `processor_running` is True or `detected_tokens_queue` has items.
        auto_buy_processor()

def _run_async_listener_with_context(app_instance):
    with app_instance.app_context():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(solana_logs_listener())
        except asyncio.CancelledError:
            logger.info("Listener's asyncio loop was cancelled.")
        finally:
            loop.close()
            logger.info("Listener's asyncio loop closed.")