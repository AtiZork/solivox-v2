from datetime import datetime

from flask_jwt_extended import jwt_required, get_jwt_identity

from models import db, AutoSnipeConfig, User
from flask import jsonify, request, Blueprint

autosnipe_bp = Blueprint('autosnipe_bp', __name__)

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
