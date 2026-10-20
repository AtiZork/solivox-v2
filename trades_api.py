"""
Action-plan 3.1: GET /api/trades - a real, filterable, paginated trade list.

Kept in its own file rather than trade_history.py, which only serves one
trade's history by id today.

P&L here is computed in USD, not SOL: Trade.initial_price/TradeHistory's
execution_price are USD-per-token (see dashboard_api.py's module docstring
for how this was confirmed), and the real on-chain SOL proceeds of a sell
are not captured anywhere in this codebase today - see dashboard_api.py for
the full explanation of what's tracked and what isn't.

status/outcome are derived, not stored columns - filtering (and the value
returned per row) comes entirely from the SQL query itself (a subquery for
realized P&L, a correlated EXISTS for "failed"), never from a second pass in
Python after paginating. Filtering after offset/limit would silently drop
rows from a page and under-report `total` whenever either filter is active -
exactly the kind of real-pagination bug the action plan's "real server-side
pagination" requirement exists to avoid.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, case, exists, func, select

from models import db, Trade, TradeHistory, Wallet

trades_api_bp = Blueprint("trades_api_bp", __name__)

MAX_PAGE_SIZE = 100
SORT_COLUMNS = {
    "created_at": Trade.created_at,
    "amount": Trade.amount,
}

# Built with sqlalchemy.select() rather than db.session.query(): the latter
# resolves db.session (an app-context-scoped proxy) immediately, which blew
# up at import time with "Working outside of application context" since
# this ran at module load, before any request (or app context) exists.
# select() only builds the SQL expression - it doesn't touch a session
# until something actually executes it, which happens per-request below.

# One row per trade_id with confirmed-sell proceeds/tokens-sold/last-sell-time
# totals, so a per-trade realized P&L (proceeds - entry_price * tokens_sold)
# and the exit price/duration can all be expressed as plain SQL columns -
# filtered, sorted, and selected in one query instead of a second Python pass.
_SELL_AGG_SUBQ = (
    select(
        TradeHistory.trade_id.label("trade_id"),
        func.sum(TradeHistory.amount * TradeHistory.execution_price).label("proceeds"),
        func.sum(TradeHistory.amount).label("tokens_sold"),
        func.max(TradeHistory.timestamp).label("last_sell_at"),
    )
    .where(TradeHistory.trade_type == "SELL", TradeHistory.status == "confirmed")
    .group_by(TradeHistory.trade_id)
    .subquery()
)

# The confirmed sell row with the latest timestamp per trade, for exit_price -
# a separate subquery because "the execution_price belonging to the max
# timestamp" isn't a plain aggregate the way sum()/max() on their own are.
_LAST_SELL_SUBQ = (
    select(
        TradeHistory.trade_id.label("trade_id"),
        TradeHistory.execution_price.label("execution_price"),
        func.row_number().over(
            partition_by=TradeHistory.trade_id, order_by=TradeHistory.timestamp.desc()
        ).label("rn"),
    )
    .where(TradeHistory.trade_type == "SELL", TradeHistory.status == "confirmed")
    .subquery()
)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@trades_api_bp.route("/api/trades", methods=["GET"])
@jwt_required()
def list_trades():
    user_id = get_jwt_identity()
    args = request.args

    pnl_expr = _SELL_AGG_SUBQ.c.proceeds - _SELL_AGG_SUBQ.c.tokens_sold * Trade.initial_price
    status_expr = case(
        (Trade.executed == True, "confirmed"),  # noqa: E712
        (Trade.sell_pending == True, "pending"),  # noqa: E712
        (
            exists().where(and_(
                TradeHistory.trade_id == Trade.id,
                TradeHistory.status.in_(["failed", "failed_timeout"]),
            )),
            "failed",
        ),
        else_="open",
    )
    query = (
        db.session.query(
            Trade,
            pnl_expr.label("pnl_usd"),
            status_expr.label("status"),
            # Raw datetime, not pre-subtracted in SQL: date-math syntax
            # differs enough between Postgres (production) and SQLite (the
            # test suite's in-memory DB, see test_route_auth.py) that doing
            # the subtraction in Python after the query - SQLAlchemy already
            # hands back real datetime objects either way - is the portable
            # choice.
            _SELL_AGG_SUBQ.c.last_sell_at,
            _LAST_SELL_SUBQ.c.execution_price.label("exit_price_usd"),
        )
        .outerjoin(_SELL_AGG_SUBQ, _SELL_AGG_SUBQ.c.trade_id == Trade.id)
        .outerjoin(
            _LAST_SELL_SUBQ,
            and_(_LAST_SELL_SUBQ.c.trade_id == Trade.id, _LAST_SELL_SUBQ.c.rn == 1),
        )
        .filter(Trade.user_id == user_id)
    )

    from_date = _parse_date(args.get("from"))
    if from_date:
        query = query.filter(Trade.created_at >= from_date)
    to_date = _parse_date(args.get("to"))
    if to_date:
        query = query.filter(Trade.created_at <= to_date)

    trade_kind = args.get("trade_kind")
    if trade_kind:
        query = query.filter(Trade.trade_kind == trade_kind)

    token = args.get("token")
    if token:
        query = query.filter(Trade.token_address.ilike(f"%{token}%"))

    wallet_id = args.get("wallet_id", type=int)
    if wallet_id:
        # Scoped to the caller's own wallet - never trust a bare id alone.
        wallet = Wallet.query.filter_by(id=wallet_id, user_id=user_id).first()
        if not wallet:
            return jsonify({"message": "Wallet not found"}), 404
        query = query.filter(Trade.to_pubkey == wallet.public_key)

    status_filter = args.get("status")
    if status_filter:
        query = query.filter(status_expr == status_filter)

    outcome_filter = args.get("outcome")
    if outcome_filter == "win":
        query = query.filter(pnl_expr > 0)
    elif outcome_filter == "loss":
        query = query.filter(pnl_expr <= 0)

    sort_key = args.get("sort", "created_at")
    descending = sort_key.startswith("-")
    bare_key = sort_key.lstrip("-")
    if bare_key in SORT_COLUMNS:
        sort_column = SORT_COLUMNS[bare_key]
        query = query.order_by(sort_column.desc() if descending else sort_column.asc())
    else:
        query = query.order_by(Trade.created_at.desc())

    page = max(1, args.get("page", 1, type=int))
    page_size = min(MAX_PAGE_SIZE, max(1, args.get("page_size", 25, type=int)))
    total = query.count()
    results = query.offset((page - 1) * page_size).limit(page_size).all()

    rows = []
    for trade, pnl_usd, status, last_sell_at, exit_price_usd in results:
        outcome = None
        if pnl_usd is not None:
            outcome = "win" if pnl_usd > 0 else "loss"
        duration_sec = (
            (last_sell_at - trade.created_at).total_seconds()
            if last_sell_at and trade.created_at else None
        )
        rows.append({
            "id": trade.id,
            "token_address": trade.token_address,
            "token_name": trade.token_name,
            "token_symbol": trade.token_symbol,
            "trade_kind": trade.trade_kind,
            "status": status,
            "entry_price_usd": trade.initial_price,
            "exit_price_usd": exit_price_usd,
            "sol_spent": trade.amount,
            "realized_pnl_usd": pnl_usd,
            "outcome": outcome,
            "duration_sec": duration_sec,
            "created_at": trade.created_at.isoformat() if trade.created_at else None,
        })

    return jsonify({
        "trades": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
    })


@trades_api_bp.route("/api/trades/log", methods=["GET"])
@jwt_required()
def transaction_log():
    """
    Client feedback: show a raw transaction log (every BUY/SELL event, not
    just the one-row-per-Trade summary GET /api/trades returns). Joins
    TradeHistory to Trade specifically to scope by user_id - TradeHistory
    itself has no owner column, only trade_id, so this is the only way to
    enforce ownership here. An inner join (not outerjoin) is deliberate: a
    TradeHistory row with a null trade_id (nullable=True in the model)
    can't be attributed to any user, so it's correctly excluded rather than
    guessed at.
    """
    user_id = get_jwt_identity()
    args = request.args

    query = (
        db.session.query(TradeHistory, Trade.token_address, Trade.token_name, Trade.token_symbol)
        .join(Trade, Trade.id == TradeHistory.trade_id)
        .filter(Trade.user_id == user_id)
    )

    from_date = _parse_date(args.get("from"))
    if from_date:
        query = query.filter(TradeHistory.timestamp >= from_date)
    to_date = _parse_date(args.get("to"))
    if to_date:
        query = query.filter(TradeHistory.timestamp <= to_date)
    trade_type = args.get("trade_type")
    if trade_type:
        query = query.filter(TradeHistory.trade_type == trade_type)
    trade_id = args.get("trade_id", type=int)
    if trade_id:
        query = query.filter(TradeHistory.trade_id == trade_id)

    page = max(1, args.get("page", 1, type=int))
    page_size = min(MAX_PAGE_SIZE, max(1, args.get("page_size", 25, type=int)))
    total = query.count()
    results = (
        query.order_by(TradeHistory.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    events = [
        {
            "id": h.id,
            "trade_id": h.trade_id,
            "trade_type": h.trade_type,
            "token_address": token_address,
            "token_name": token_name,
            "token_symbol": token_symbol,
            "amount": h.amount,
            "execution_price_usd": h.execution_price,
            "tx_id": h.tx_id,
            "status": h.status,
            "timestamp": h.timestamp.isoformat() if h.timestamp else None,
        }
        for h, token_address, token_name, token_symbol in results
    ]

    return jsonify({
        "events": events,
        "page": page,
        "page_size": page_size,
        "total": total,
    })
