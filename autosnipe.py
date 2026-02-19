from flask_jwt_extended import jwt_required, get_jwt_identity
from autosnipe_logic import (
    set_listener_running_status,
    set_processor_running_status,
    solana_logs_listener,
    auto_buy_processor,
)
from models import db, AutoSnipeConfig, User, TradeLog
import threading
import asyncio
import logging
from flask import request, jsonify, Blueprint, current_app
from datetime import datetime
import sqlalchemy

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


def _validate_config_payload(data):
    """Validate incoming payload for autosnipe create/update. Returns list of errors."""
    errors = []
    # numeric checks
    numeric_fields = ['buy_txns_over_80_usd','min_txns','launch_delay','buy_amount','slippage','priority_fee']
    for f in numeric_fields:
        if f in data and data.get(f) is not None:
            try:
                float(data.get(f))
            except Exception:
                errors.append(f"{f} must be numeric")
    return errors


# Backwards-compatible single-config save (keeps existing front-end POST /api/autosnipe)
@autosnipe_bp.route('/api/autosnipe', methods=['POST'])
@jwt_required()
def autosnipe():
    try:
        user_id = get_jwt_identity()
        data = request.get_json() or {}

        # validate
        errors = _validate_config_payload(data)
        if errors:
            return jsonify({'errors': errors}), 400

        # For compatibility: if 'id' provided, update that config, otherwise create a new config for user
        config_id = data.get('id')
        if config_id:
            config = AutoSnipeConfig.query.filter_by(id=config_id, user_id=user_id).first()
            if not config:
                return jsonify({'error': 'Config not found'}), 404
        else:
            # create a new config for this user (allow multiple AutoSnipers)
            config = AutoSnipeConfig(user_id=user_id)

        active = data.get("active", True)  # default True if missing
        if isinstance(active, str):
            active = active.lower() == "true"

        config.active = bool(active)
        # Direct assign assuming correct types from frontend (coerce where reasonable)
        if 'buy_txns_over_80_usd' in data:
            config.buy_txns_over_80_usd = int(data.get('buy_txns_over_80_usd'))
        if 'min_txns' in data:
            config.min_txns = int(data.get('min_txns'))
        if 'launch_delay' in data:
            config.launch_delay = int(data.get('launch_delay'))
        if 'buy_amount' in data:
            config.buy_amount = float(data.get('buy_amount'))
        if 'slippage' in data:
            config.slippage = float(data.get('slippage'))
        if 'priority_fee' in data:
            config.priority_fee = float(data.get('priority_fee'))

        # sell-related
        for f in ['drop_cutoff','drop_until_profit','drop_after_100','drop_after_400',
                  'sell_at_200','sell_at_400','sell_at_1000','sell_at_1500','sell_at_2500','sell_at_4000','sell_at_10000']:
            if f in data:
                try:
                    setattr(config, f, float(data.get(f)))
                except Exception:
                    pass

        # `name` field intentionally removed per request; do not assign
        config.user_id = int(user_id)

        config.timestamp = datetime.utcnow()

        db.session.add(config)
        db.session.commit()

        return jsonify({'message': 'AutoSnipe config saved successfully', 'id': config.id}), 200
    except Exception as e:
        db.session.rollback()
        logger.exception("Error saving AutoSnipe config")
        return jsonify({'error': 'Failed to save AutoSnipe config'}), 500


# New endpoints for managing multiple AutoSnipers
@autosnipe_bp.route('/api/autosnipe/list', methods=['GET'])
@jwt_required()
def list_autosnipers():
    user_id = get_jwt_identity()
    try:
        configs = AutoSnipeConfig.query.filter_by(user_id=user_id).order_by(AutoSnipeConfig.id.desc()).all()
        return jsonify([c.to_dict() for c in configs]), 200
    except sqlalchemy.exc.ProgrammingError as pe:
        # Schema mismatch (missing columns) — fallback: reflect available columns and return minimal rows
        logger.warning('Schema mismatch when querying AutoSnipeConfig, falling back to raw table read: %s', pe)
        table_name = getattr(AutoSnipeConfig, '__tablename__', None) or AutoSnipeConfig.__table__.name
        try:
            insp = sqlalchemy.inspect(db.engine)
            if table_name not in insp.get_table_names():
                return jsonify([]), 200
            # Fetch rows using raw SQL, map only available columns
            with db.engine.connect() as conn:
                result = conn.execute(sqlalchemy.text(f"SELECT * FROM {table_name} WHERE user_id = :uid ORDER BY timestamp DESC"), {'uid': user_id})
                rows = []
                for r in result:
                    row = dict(r)
                    # Map to expected output keys with sensible defaults
                    mapped = {
                        'id': row.get('id'),
                        'active': bool(row.get('active')) if 'active' in row else True,
                        'buy_txns_over_80_usd': row.get('buy_txns_over_80_usd', 80),
                        'min_txns': row.get('min_txns', 5),
                        'launch_delay': row.get('launch_delay', 5),
                        'buy_amount': row.get('buy_amount', 1.0),
                        'slippage': row.get('slippage', 100),
                        'priority_fee': row.get('priority_fee', 0.01),
                        'drop_cutoff': row.get('drop_cutoff', 30),
                        'drop_until_profit': row.get('drop_until_profit', 99),
                        'drop_after_100': row.get('drop_after_100', 50),
                        'drop_after_400': row.get('drop_after_400', 30),
                        'sell_at_200': row.get('sell_at_200', 10),
                        'sell_at_400': row.get('sell_at_400', 10),
                        'sell_at_1000': row.get('sell_at_1000', 10),
                        'sell_at_1500': row.get('sell_at_1500', 10),
                        'sell_at_2500': row.get('sell_at_2500', 10),
                        'sell_at_4000': row.get('sell_at_4000', 10),
                        'sell_at_10000': row.get('sell_at_10000', 10),
                        'timestamp': row.get('timestamp').isoformat() if row.get('timestamp') else None,
                    }
                    rows.append(mapped)
                return jsonify(rows), 200
        except Exception as e:
            logger.exception('Fallback read failed')
            return jsonify([]), 200
    except Exception as e:
        logger.exception('Error listing autosnipers')
        return jsonify([]), 200


@autosnipe_bp.route('/api/autosnipe', methods=['GET'])
@jwt_required()
def get_autosnipe():
    # Keep existing single-get for compatibility: returns first config if exists
    user_id = get_jwt_identity()
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({"msg": "User not found"}), 404
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
                "id": config.id,
            })
        return jsonify({})
    except Exception as exc:
        # Handle schema mismatch (e.g., missing expected columns) by doing a raw fallback read
        logger.warning('Error in GET /api/autosnipe: %s. Attempting fallback raw read.', exc)
        table_name = getattr(AutoSnipeConfig, '__tablename__', None) or AutoSnipeConfig.__table__.name
        try:
            insp = sqlalchemy.inspect(db.engine)
            if table_name not in insp.get_table_names():
                return jsonify({}), 200
            with db.engine.connect() as conn:
                # Fetch single row for user
                result = conn.execute(sqlalchemy.text(f"SELECT * FROM {table_name} WHERE user_id = :uid ORDER BY timestamp DESC LIMIT 1"), {'uid': user_id})
                row = result.fetchone()
                if not row:
                    return jsonify({}), 200
                r = dict(row)
                mapped = {
                    'active': bool(r.get('active')) if 'active' in r else True,
                    'buy_txns_over_80_usd': r.get('buy_txns_over_80_usd', 80),
                    'min_txns': r.get('min_txns', 5),
                    'launch_delay': r.get('launch_delay', 5),
                    'buy_amount': r.get('buy_amount', 1.0),
                    'slippage': r.get('slippage', 100),
                    'priority_fee': r.get('priority_fee', 0.01),
                    'drop_cutoff': r.get('drop_cutoff', 30),
                    'drop_until_profit': r.get('drop_until_profit', 99),
                    'drop_after_100': r.get('drop_after_100', 50),
                    'drop_after_400': r.get('drop_after_400', 30),
                    'sell_at_200': r.get('sell_at_200', 10),
                    'sell_at_400': r.get('sell_at_400', 10),
                    'sell_at_1000': r.get('sell_at_1000', 10),
                    'sell_at_1500': r.get('sell_at_1500', 10),
                    'sell_at_2500': r.get('sell_at_2500', 10),
                    'sell_at_4000': r.get('sell_at_4000', 10),
                    'sell_at_10000': r.get('sell_at_10000', 10),
                    'id': r.get('id'),
                }
                return jsonify(mapped), 200
        except Exception:
            logger.exception('Fallback read for GET /api/autosnipe failed')
            return jsonify({}), 200


@autosnipe_bp.route('/api/autosnipe/<int:config_id>', methods=['GET'])
@jwt_required()
def get_autosnipe_by_id(config_id):
    user_id = get_jwt_identity()
    config = AutoSnipeConfig.query.filter_by(id=config_id, user_id=user_id).first()
    if not config:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(config.to_dict()), 200


@autosnipe_bp.route('/api/autosnipe/<int:config_id>', methods=['PUT'])
@jwt_required()
def update_autosnipe(config_id):
    try:
        user_id = get_jwt_identity()
        data = request.get_json() or {}
        # validate
        errors = _validate_config_payload(data)
        if errors:
            return jsonify({'errors': errors}), 400
        config = AutoSnipeConfig.query.filter_by(id=config_id, user_id=user_id).first()
        if not config:
            return jsonify({'error': 'Not found'}), 404
        # Update allowed fields
        if 'active' in data:
            config.active = bool(data.get('active', config.active))
        for f in ['buy_txns_over_80_usd','min_txns','launch_delay','buy_amount','slippage','priority_fee']:
            if f in data:
                try:
                    setattr(config, f, float(data.get(f)))
                except Exception:
                    pass
        for f in ['drop_cutoff','drop_until_profit','drop_after_100','drop_after_400',
                  'sell_at_200','sell_at_400','sell_at_1000','sell_at_1500','sell_at_2500','sell_at_4000','sell_at_10000']:
            if f in data:
                try:
                    setattr(config, f, float(data.get(f)))
                except Exception:
                    pass
        config.timestamp = datetime.utcnow()
        db.session.add(config)
        db.session.commit()
        return jsonify(config.to_dict()), 200
    except Exception as e:
        db.session.rollback()
        logger.exception("Error updating config")
        return jsonify({'error': 'Failed to update'}), 500


@autosnipe_bp.route('/api/autosnipe/<int:config_id>', methods=['DELETE'])
@jwt_required()
def delete_autosnipe(config_id):
    try:
        user_id = get_jwt_identity()
        config = AutoSnipeConfig.query.filter_by(id=config_id, user_id=user_id).first()
        if not config:
            return jsonify({'error': 'Not found'}), 404
        db.session.delete(config)
        db.session.commit()
        return jsonify({}), 204
    except Exception as e:
        db.session.rollback()
        logger.exception("Error deleting config")
        return jsonify({'error': 'Failed to delete'}), 500


@autosnipe_bp.route('/api/autosnipe/<int:config_id>/toggle', methods=['PATCH'])
@jwt_required()
def toggle_autosnipe_active(config_id):
    """Toggle active state for a sniper (fast on-card toggle endpoint)."""
    try:
        user_id = get_jwt_identity()
        config = AutoSnipeConfig.query.filter_by(id=config_id, user_id=user_id).first()
        if not config:
            return jsonify({'error': 'Not found'}), 404
        # toggle or set explicitly
        data = request.get_json() or {}
        if 'active' in data:
            config.active = bool(data.get('active'))
        else:
            config.active = not config.active
        config.timestamp = datetime.utcnow()
        db.session.add(config)
        db.session.commit()
        return jsonify({'id': config.id, 'active': config.active}), 200
    except Exception as e:
        db.session.rollback()
        logger.exception("Error toggling config active")
        return jsonify({'error': 'Failed to toggle active'}), 500


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