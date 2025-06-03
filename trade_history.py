from flask import Blueprint, jsonify
from models import db, Trade, TradeHistory

trade_history_bp = Blueprint("trade_history", __name__)


@trade_history_bp.route("/trade_history/<int:trade_id>", methods=["GET"])
def get_trade_history(trade_id):
    try:
        # Fetch all history entries for this trade
        records = (
            TradeHistory.query
            .filter_by(trade_id=trade_id)
            .order_by(TradeHistory.timestamp.desc())
            .all()
        )

        if not records:
            # return jsonify({"error": "No history found for this trade ID"}), 404
            return jsonify({"history": []}), 200

        history = [
            {
                "id": r.id,
                "trade_type": r.trade_type,
                "amount": r.amount,
                "execution_price": r.execution_price,
                "tx_id": r.tx_id,
                "timestamp": r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for r in records
        ]

        return jsonify({
            "trade_id": trade_id,
            "token_address": records[0].token_address,
            "history": history
        }), 200

    except Exception as e:
        return jsonify({"message": str(e)}), 500
