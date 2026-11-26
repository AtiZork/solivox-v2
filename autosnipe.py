from flask_jwt_extended import jwt_required, get_jwt_identity
from models import db, AutoSnipeConfig, User, TradeLog, Wallet
import logging
from flask import request, jsonify, Blueprint
from datetime import datetime
import sqlalchemy

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


# (field, min, max, max exclusive?) - min/max are the sane bounds for each
# setting. Previously only numeric-ness was checked, so e.g. min_txns=0
# (every launch instantly qualifies), buy_amount=-1, or slippage=1000 were
# all silently accepted. Bounds below are a starting point, not fixed
# business rules - adjust them if the real strategy needs a wider range.
_RANGE_FIELDS = {
    # 0 would mean "every new token instantly passes the buy-count filter".
    'min_txns': (1, 1000),
    'buy_txns_over_80_usd': (0, 100_000),
    # Optional ceiling - see AutoSnipeConfig.buy_txns_max_usd. Cross-field
    # (must be >= buy_txns_over_80_usd) check is separate, below.
    'buy_txns_max_usd': (0, 100_000),
    'launch_delay': (0, 300),
    # Upper bound is a fat-finger guard (a typo like 100 SOL instead of 1),
    # not a strategy limit - must be > 0 so the bot can't "buy" nothing.
    'buy_amount': (0, 100),
    # 100 is the real ceiling (100% slippage = accepts any output amount) -
    # the client's strategy intentionally wants the full range available,
    # so this isn't capped any lower.
    'slippage': (0, 100),
    'priority_fee': (0, 1),
    'sell_priority_fee': (0, 1),
    'drop_cutoff': (0, 100),
    'drop_until_profit': (0, 100),
    'drop_after_100': (0, 100),
    'drop_after_400': (0, 100),
    'sell_at_100': (0, 100), 'sell_at_200': (0, 100), 'sell_at_300': (0, 100),
    'sell_at_400': (0, 100), 'sell_at_600': (0, 100), 'sell_at_800': (0, 100),
    'sell_at_1000': (0, 100), 'sell_at_1500': (0, 100), 'sell_at_2500': (0, 100),
    'sell_at_4000': (0, 100), 'sell_at_10000': (0, 100),
    'inactivity_sell_seconds': (0, 3600),
    'critical_failsafe_seconds': (0, 3600),
}
# Fields whose lower bound is exclusive (must be strictly greater than, not
# just "at least") - these are the ones that are meaningless or dangerous at
# exactly zero: no buy, or launch-delay semantics up above already cover 0.
_EXCLUSIVE_MIN_FIELDS = {'buy_amount', 'slippage'}


def _validate_config_payload(data):
    """Validate incoming payload for autosnipe create/update. Returns list of errors."""
    from autosnipe_buy_logic import validate_buy_amount_range, validate_half_txns_scan_duration

    errors = []
    # numeric checks
    numeric_fields = [
        'buy_txns_over_80_usd', 'buy_txns_max_usd', 'min_txns', 'launch_delay', 'buy_amount',
        'slippage', 'priority_fee', 'sell_priority_fee', 'half_txns_scan_duration',
    ]
    parsed = {}
    for f in numeric_fields:
        if f in data and data.get(f) is not None:
            try:
                parsed[f] = float(data.get(f))
            except Exception:
                errors.append(f"{f} must be numeric")

    # Range checks - covers both the fields above and the sell-side fields
    # (drop_cutoff, sell_at_*, ...) that previously had no validation at all.
    for f, (lo, hi) in _RANGE_FIELDS.items():
        if f not in data or data.get(f) is None:
            continue
        if f in parsed:
            value = parsed[f]
        else:
            try:
                value = float(data.get(f))
            except Exception:
                errors.append(f"{f} must be numeric")
                continue
        too_low = value <= lo if f in _EXCLUSIVE_MIN_FIELDS else value < lo
        if too_low or value > hi:
            errors.append(f"{f} must be between {lo} and {hi}")

    # buy_txns_max_usd is mandatory (client requirement, reversed from the
    # original optional/unbounded-by-default design): any payload that
    # touches the buy-amount range at all - setting the min, or touching the
    # max field itself (including trying to clear it back to blank) - must
    # end up with a real max value. A payload that doesn't touch either
    # field (e.g. a save only changing sell-side settings) is unaffected.
    if 'buy_txns_over_80_usd' in data or 'buy_txns_max_usd' in data:
        raw_max = data.get('buy_txns_max_usd')
        if raw_max in (None, ''):
            errors.append(
                "buy_txns_max_usd is required - maximum transaction amount must be set"
            )

    # Only meaningful when both bounds are present in *this* payload - a
    # request only touching one of the two fields has no reliable way to
    # cross-check against the other's already-saved value here.
    if 'buy_txns_over_80_usd' in data and 'buy_txns_max_usd' in data:
        errors.extend(
            validate_buy_amount_range(
                data.get('buy_txns_over_80_usd'),
                data.get('buy_txns_max_usd'),
            )
        )

    half_enabled = False
    if 'half_txns_scan_enabled' in data:
        half_enabled = _parse_bool(data.get('half_txns_scan_enabled'), False)
    errors.extend(
        validate_half_txns_scan_duration(
            data.get('half_txns_scan_duration'),
            enabled=half_enabled,
        )
    )
    return errors


def _parse_bool(value, default=True):
    """Parse JSON/form boolean values, matching AutoSnipeConfig.active handling."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


def _resolve_wallet_id(data, user_id, errors):
    """Validates 'wallet_id' against the caller's own wallets if present.

    Returns (should_set, value): should_set is False when the field wasn't
    in the payload at all (leave the config's existing wallet_id alone);
    value is None to explicitly clear it, or the validated wallet id.
    """
    if 'wallet_id' not in data:
        return False, None
    raw = data.get('wallet_id')
    if raw in (None, ''):
        return True, None
    try:
        wallet_id = int(raw)
    except Exception:
        errors.append('wallet_id must be an integer')
        return False, None
    if not Wallet.query.filter_by(id=wallet_id, user_id=user_id).first():
        errors.append('wallet_id must be one of your own wallets')
        return False, None
    return True, wallet_id


_SNIPER_ENABLE_FIELDS = (
    "drop_cutoff_enabled",
    "drop_after_100_enabled",
    "drop_after_400_enabled",
    "inactivity_sell_enabled",
    "critical_failsafe_enabled",
)


# Backwards-compatible single-config save (keeps existing front-end POST /api/autosnipe)
@autosnipe_bp.route('/api/autosnipe', methods=['POST'])
@jwt_required()
def autosnipe():
    try:
        user_id = get_jwt_identity()
        data = request.get_json() or {}

        # validate
        errors = _validate_config_payload(data)
        set_wallet, wallet_id_value = _resolve_wallet_id(data, user_id, errors)
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

        if set_wallet:
            config.wallet_id = wallet_id_value

        active = data.get("active", True)  # default True if missing
        config.active = _parse_bool(active, True)
        # Direct assign assuming correct types from frontend (coerce where reasonable)
        if 'buy_txns_over_80_usd' in data:
            config.buy_txns_over_80_usd = int(data.get('buy_txns_over_80_usd'))
        if 'buy_txns_max_usd' in data:
            raw_max = data.get('buy_txns_max_usd')
            config.buy_txns_max_usd = float(raw_max) if raw_max not in (None, '') else None
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
        if 'sell_priority_fee' in data:
            config.sell_priority_fee = float(data.get('sell_priority_fee'))
        if 'half_txns_scan_enabled' in data:
            config.half_txns_scan_enabled = _parse_bool(data.get('half_txns_scan_enabled'), False)
        if 'half_txns_scan_duration' in data and data.get('half_txns_scan_duration') is not None:
            config.half_txns_scan_duration = int(data.get('half_txns_scan_duration'))

        # sell-related
        for f in ['drop_cutoff','drop_until_profit','drop_after_100','drop_after_400',
                  'sell_at_100','sell_at_200','sell_at_300','sell_at_400','sell_at_600','sell_at_800',
                  'sell_at_1000','sell_at_1500','sell_at_2500','sell_at_4000','sell_at_10000',
                  'inactivity_sell_seconds', 'critical_failsafe_seconds']:
            if f in data:
                try:
                    setattr(config, f, float(data.get(f)))
                except Exception:
                    pass
        for f in _SNIPER_ENABLE_FIELDS:
            if f in data:
                setattr(config, f, _parse_bool(data.get(f), True))

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
                        'wallet_id': row.get('wallet_id'),
                        'buy_txns_over_80_usd': row.get('buy_txns_over_80_usd', 80),
                        'buy_txns_max_usd': row.get('buy_txns_max_usd'),
                        'min_txns': row.get('min_txns', 5),
                        'launch_delay': row.get('launch_delay', 5),
                        'half_txns_scan_enabled': bool(row.get('half_txns_scan_enabled')) if 'half_txns_scan_enabled' in row else False,
                        'half_txns_scan_duration': row.get('half_txns_scan_duration', 30),
                        'buy_amount': row.get('buy_amount', 1.0),
                        'slippage': row.get('slippage', 20),
                        'priority_fee': row.get('priority_fee', 0.01),
                        'sell_priority_fee': row.get('sell_priority_fee', 0.01),
                        'drop_cutoff': row.get('drop_cutoff', 30),
                        'drop_cutoff_enabled': bool(row.get('drop_cutoff_enabled')) if 'drop_cutoff_enabled' in row else True,
                        'drop_until_profit': row.get('drop_until_profit', 99),
                        'drop_after_100': row.get('drop_after_100', 50),
                        'drop_after_100_enabled': bool(row.get('drop_after_100_enabled')) if 'drop_after_100_enabled' in row else True,
                        'drop_after_400': row.get('drop_after_400', 30),
                        'drop_after_400_enabled': bool(row.get('drop_after_400_enabled')) if 'drop_after_400_enabled' in row else True,
                        'sell_at_100': row.get('sell_at_100', 10),
                        'sell_at_200': row.get('sell_at_200', 10),
                        'sell_at_300': row.get('sell_at_300', 10),
                        'sell_at_400': row.get('sell_at_400', 10),
                        'sell_at_600': row.get('sell_at_600', 10),
                        'sell_at_800': row.get('sell_at_800', 10),
                        'sell_at_1000': row.get('sell_at_1000', 10),
                        'sell_at_1500': row.get('sell_at_1500', 10),
                        'sell_at_2500': row.get('sell_at_2500', 10),
                        'sell_at_4000': row.get('sell_at_4000', 10),
                        'sell_at_10000': row.get('sell_at_10000', 10),
                        'inactivity_sell_enabled': bool(row.get('inactivity_sell_enabled')) if 'inactivity_sell_enabled' in row else True,
                        'inactivity_sell_seconds': row.get('inactivity_sell_seconds', 5),
                        'critical_failsafe_enabled': bool(row.get('critical_failsafe_enabled')) if 'critical_failsafe_enabled' in row else True,
                        'critical_failsafe_seconds': row.get('critical_failsafe_seconds', 5),
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
                "wallet_id": config.wallet_id,
                "buy_txns_over_80_usd": config.buy_txns_over_80_usd,
                "buy_txns_max_usd": getattr(config, "buy_txns_max_usd", None),
                "min_txns": config.min_txns,
                "launch_delay": config.launch_delay,
                "half_txns_scan_enabled": bool(getattr(config, "half_txns_scan_enabled", False)),
                "half_txns_scan_duration": getattr(config, "half_txns_scan_duration", 30),
                "buy_amount": config.buy_amount,
                "slippage": config.slippage,
                "priority_fee": config.priority_fee,
                "sell_priority_fee": getattr(config, "sell_priority_fee", 0.01),
                "drop_cutoff": config.drop_cutoff,
                "drop_cutoff_enabled": bool(getattr(config, "drop_cutoff_enabled", True)),
                "drop_until_profit": config.drop_until_profit,
                "drop_after_100": config.drop_after_100,
                "drop_after_100_enabled": bool(getattr(config, "drop_after_100_enabled", True)),
                "drop_after_400": config.drop_after_400,
                "drop_after_400_enabled": bool(getattr(config, "drop_after_400_enabled", True)),
                "sell_at_100": getattr(config, "sell_at_100", 10),
                "sell_at_200": config.sell_at_200,
                "sell_at_300": getattr(config, "sell_at_300", 10),
                "sell_at_400": config.sell_at_400,
                "sell_at_600": getattr(config, "sell_at_600", 10),
                "sell_at_800": getattr(config, "sell_at_800", 10),
                "sell_at_1000": config.sell_at_1000,
                "sell_at_1500": config.sell_at_1500,
                "sell_at_2500": config.sell_at_2500,
                "sell_at_4000": config.sell_at_4000,
                "sell_at_10000": config.sell_at_10000,
                "inactivity_sell_enabled": bool(getattr(config, "inactivity_sell_enabled", True)),
                "inactivity_sell_seconds": getattr(config, "inactivity_sell_seconds", 5),
                "critical_failsafe_enabled": bool(getattr(config, "critical_failsafe_enabled", True)),
                "critical_failsafe_seconds": getattr(config, "critical_failsafe_seconds", 5),
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
                    'wallet_id': r.get('wallet_id'),
                    'buy_txns_over_80_usd': r.get('buy_txns_over_80_usd', 80),
                    'buy_txns_max_usd': r.get('buy_txns_max_usd'),
                    'min_txns': r.get('min_txns', 5),
                    'launch_delay': r.get('launch_delay', 5),
                    'half_txns_scan_enabled': bool(r.get('half_txns_scan_enabled')) if 'half_txns_scan_enabled' in r else False,
                    'half_txns_scan_duration': r.get('half_txns_scan_duration', 30),
                    'buy_amount': r.get('buy_amount', 1.0),
                    'slippage': r.get('slippage', 20),
                    'priority_fee': r.get('priority_fee', 0.01),
                    'sell_priority_fee': r.get('sell_priority_fee', 0.01),
                    'drop_cutoff': r.get('drop_cutoff', 30),
                    'drop_cutoff_enabled': bool(r.get('drop_cutoff_enabled')) if 'drop_cutoff_enabled' in r else True,
                    'drop_until_profit': r.get('drop_until_profit', 99),
                    'drop_after_100': r.get('drop_after_100', 50),
                    'drop_after_100_enabled': bool(r.get('drop_after_100_enabled')) if 'drop_after_100_enabled' in r else True,
                    'drop_after_400': r.get('drop_after_400', 30),
                    'drop_after_400_enabled': bool(r.get('drop_after_400_enabled')) if 'drop_after_400_enabled' in r else True,
                    'sell_at_100': r.get('sell_at_100', 10),
                    'sell_at_200': r.get('sell_at_200', 10),
                    'sell_at_300': r.get('sell_at_300', 10),
                    'sell_at_400': r.get('sell_at_400', 10),
                    'sell_at_600': r.get('sell_at_600', 10),
                    'sell_at_800': r.get('sell_at_800', 10),
                    'sell_at_1000': r.get('sell_at_1000', 10),
                    'sell_at_1500': r.get('sell_at_1500', 10),
                    'sell_at_2500': r.get('sell_at_2500', 10),
                    'sell_at_4000': r.get('sell_at_4000', 10),
                    'sell_at_10000': r.get('sell_at_10000', 10),
                    'inactivity_sell_enabled': bool(r.get('inactivity_sell_enabled')) if 'inactivity_sell_enabled' in r else True,
                    'inactivity_sell_seconds': r.get('inactivity_sell_seconds', 5),
                    'critical_failsafe_enabled': bool(r.get('critical_failsafe_enabled')) if 'critical_failsafe_enabled' in r else True,
                    'critical_failsafe_seconds': r.get('critical_failsafe_seconds', 5),
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
        set_wallet, wallet_id_value = _resolve_wallet_id(data, user_id, errors)
        if errors:
            return jsonify({'errors': errors}), 400
        config = AutoSnipeConfig.query.filter_by(id=config_id, user_id=user_id).first()
        if not config:
            return jsonify({'error': 'Not found'}), 404
        if set_wallet:
            config.wallet_id = wallet_id_value
        # Update allowed fields
        if 'active' in data:
            config.active = _parse_bool(data.get('active'), config.active)
        for f in ['buy_txns_over_80_usd','min_txns','launch_delay','buy_amount','slippage','priority_fee','sell_priority_fee']:
            if f in data:
                try:
                    setattr(config, f, float(data.get(f)))
                except Exception:
                    pass
        if 'buy_txns_max_usd' in data:
            raw_max = data.get('buy_txns_max_usd')
            try:
                config.buy_txns_max_usd = float(raw_max) if raw_max not in (None, '') else None
            except Exception:
                pass
        if 'half_txns_scan_enabled' in data:
            config.half_txns_scan_enabled = _parse_bool(data.get('half_txns_scan_enabled'), False)
        if 'half_txns_scan_duration' in data and data.get('half_txns_scan_duration') is not None:
            try:
                config.half_txns_scan_duration = int(data.get('half_txns_scan_duration'))
            except Exception:
                pass
        for f in ['drop_cutoff','drop_until_profit','drop_after_100','drop_after_400',
                  'sell_at_100','sell_at_200','sell_at_300','sell_at_400','sell_at_600','sell_at_800',
                  'sell_at_1000','sell_at_1500','sell_at_2500','sell_at_4000','sell_at_10000',
                  'inactivity_sell_seconds', 'critical_failsafe_seconds']:
            if f in data:
                try:
                    setattr(config, f, float(data.get(f)))
                except Exception:
                    pass
        for f in _SNIPER_ENABLE_FIELDS:
            if f in data:
                setattr(config, f, _parse_bool(data.get(f), True))
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
    """Retired: used to start a second, unauthenticated buy bot
    (autosnipe_logic.auto_buy_processor) that picked an arbitrary user's
    wallet/config via Wallet.query.first() / AutoSnipeConfig.query.first()
    instead of the caller's own, and created Trade rows without user_id (a
    NOT NULL column) - so its DB write threw after a real on-chain buy had
    already been sent, leaving an unrecorded, unsellable position. The real
    sniper (autosnipe_buy_new_token.start_sniper_ingestion, started from
    main.py) already covers token sniping independently of this route.
    autosnipe_logic.py (the module this used to call into) has been deleted
    along with the thread-management code that lived here.
    """
    return jsonify({
        "status": "error",
        "message": "This endpoint has been retired. The sniper runs automatically; "
                    "use the AutoSnipe config's Active toggle instead.",
    }), 410


@autosnipe_bp.route('/stop_all', methods=['POST'])
def stop_all_bots():
    """Retired along with /start_all - see start_all_bots() docstring."""
    return jsonify({
        "status": "error",
        "message": "This endpoint has been retired. Use the AutoSnipe config's Active toggle instead.",
    }), 410