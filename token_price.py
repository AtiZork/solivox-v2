from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from models import TokenPrice, Trade

token_price_bp = Blueprint("token_price_bp", __name__)


@token_price_bp.route("/api/token/prices", methods=["GET"])
@jwt_required()
def get_all_token_prices():
    user_id = get_jwt_identity()
    trades = Trade.query.filter_by(user_id=user_id).all()
    result = []

    for trade in trades:
        # prices = TokenPrice.query.filter_by(trade_id=trade.id).order_by(TokenPrice.timestamp.desc()).all()
        prices = TokenPrice.query.filter_by(trade_id=trade.id).all()

        result.append({
            "trade_id": trade.id,
            "token_address": trade.token_address,
            "token_name": trade.token_name,
            "symbol": trade.token_symbol,
            "prices": [
                {
                    "id": p.id,
                    "price": p.price,
                    "timestamp": p.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                } for p in prices
            ]
        })

    return jsonify(result)