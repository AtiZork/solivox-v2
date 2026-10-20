"""
Action-plan 3.2 + 3.3: GET /api/dashboard/financial and GET /api/dashboard/latency.

Money semantics (traced through the actual buy/sell code, not assumed):
  - Trade.amount / TradeHistory.amount (BUY rows) is the *configured*
    buy_amount in SOL - this is what was actually swapped in (the input
    side of the buy's quote), so it's a real, exact sol_spent figure.
  - TradeHistory.amount (SELL rows) is sold_tokens - a TOKEN amount, not SOL.
  - execution_price / Trade.initial_price are USD-per-token (confirmed via
    shyft_pricing.get_token_price / yellowstone_pricing.get_yellowstone_price
    return shapes).
  - The sell quote's real on-chain SOL proceeds (its outAmount) and the real
    confirmed-tx fee are NOT captured anywhere in this codebase today - only
    *configured* settings (priority_fee/sell_priority_fee) exist, and the
    plan's own rule says not to present those as "actual" (same bug class as
    B2/C4 before they were fixed).

Decision (confirmed with the user before building this): ship the dashboard
now using only data that already exists. Realized/unrealized P&L is reported
in USD (exact), sol_spent is reported in SOL (exact, from the BUY side).
sol_received and fees_paid_sol are always null with a sibling
"not_tracked": true flag - never a plausible-but-wrong number. The gap has
its own NOT_DONE_FIXES.md entry for a future, separately-scoped pass that
would touch the buy/sell transaction code itself.

Follow-up (client feedback): also show the USD figures converted to an
*approximate* SOL equivalent, explicitly marked as such. This is a single
*current* SOL/USD rate (cached process-wide by shyft_pricing - see
get_sol_usd's own docstring, which already exists for the sell bot's hot
path, so this adds no new network calls) applied as a blanket conversion
factor to USD amounts that were actually realized at many different points
in time, each at its own real rate. It will drift from the true figure the
more the SOL price has moved since - that's exactly why it's approximate,
not exact, and the frontend prefixes it with "~". It is not a substitute for
the sol_received tracking gap above; it's a rough, honestly-labeled
conversion of the USD figures that already are exact.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, select

from models import db, Trade, TradeHistory, Wallet, TokenPrice
from dashboard_models import LatencySnapshot
from shyft_pricing import get_shyft_pricing_service

dashboard_api_bp = Blueprint("dashboard_api_bp", __name__)

# Same shape as trades_api.py's _SELL_AGG_SUBQ (built with select(), not
# db.session.query() - see that file's comment for why: this runs at import
# time, before any app context exists).
_SELL_AGG_SUBQ = (
    select(
        TradeHistory.trade_id.label("trade_id"),
        func.sum(TradeHistory.amount * TradeHistory.execution_price).label("proceeds"),
        func.sum(TradeHistory.amount).label("tokens_sold"),
    )
    .where(TradeHistory.trade_type == "SELL", TradeHistory.status == "confirmed")
    .group_by(TradeHistory.trade_id)
    .subquery()
)

# Most recent known price per trade (from either pricing scheduler - see
# init_scheduler.py / price_stream.py, both already write TokenPrice), used
# for open positions' unrealized P&L without this endpoint making its own
# live price calls.
_LATEST_PRICE_SUBQ = (
    select(
        TokenPrice.trade_id.label("trade_id"),
        TokenPrice.price.label("price"),
        func.row_number().over(
            partition_by=TokenPrice.trade_id, order_by=TokenPrice.timestamp.desc()
        ).label("rn"),
    ).subquery()
)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class _WalletNotFound(Exception):
    """Raised when wallet_id is given but doesn't belong to the caller."""


def _resolve_wallet_public_key(args, user_id) -> str | None:
    wallet_id = args.get("wallet_id", type=int)
    if not wallet_id:
        return None
    wallet = Wallet.query.filter_by(id=wallet_id, user_id=user_id).first()
    if wallet is None:
        raise _WalletNotFound()
    return wallet.public_key


def _apply_common_filters(query, args, wallet_public_key):
    from_date = _parse_date(args.get("from"))
    if from_date:
        query = query.filter(Trade.created_at >= from_date)
    to_date = _parse_date(args.get("to"))
    if to_date:
        query = query.filter(Trade.created_at <= to_date)
    trade_kind = args.get("trade_kind")
    if trade_kind:
        query = query.filter(Trade.trade_kind == trade_kind)
    if wallet_public_key:
        query = query.filter(Trade.to_pubkey == wallet_public_key)
    return query


def _apply_status_filter(query, status: str):
    if status == "confirmed":
        return query.filter(Trade.executed == True)  # noqa: E712
    if status == "open":
        return query.filter(Trade.executed == False)  # noqa: E712
    return query  # "all" (or anything else): no extra filter


@dashboard_api_bp.route("/api/dashboard/financial", methods=["GET"])
@jwt_required()
def financial_summary():
    user_id = get_jwt_identity()
    args = request.args
    status = args.get("status", "confirmed")

    try:
        wallet_public_key = _resolve_wallet_public_key(args, user_id)
    except _WalletNotFound:
        return jsonify({"message": "Wallet not found"}), 404

    pnl_expr = _SELL_AGG_SUBQ.c.proceeds - _SELL_AGG_SUBQ.c.tokens_sold * Trade.initial_price

    base = (
        # "Earned" (plan's own 4-tile wording) is the gross USD proceeds from
        # confirmed sells - distinct from realized_pnl_usd (net of cost
        # basis). Both are exact: proceeds = sum(sold_tokens * sell price).
        db.session.query(Trade, pnl_expr.label("pnl_usd"), _SELL_AGG_SUBQ.c.proceeds.label("earned_usd"))
        .outerjoin(_SELL_AGG_SUBQ, _SELL_AGG_SUBQ.c.trade_id == Trade.id)
        .filter(Trade.user_id == user_id)
    )
    base = _apply_status_filter(_apply_common_filters(base, args, wallet_public_key), status)

    rows = base.all()
    trade_count = len(rows)
    sol_spent = sum((t.amount or 0.0) for t, _, _ in rows)
    earned_usd = sum((earned or 0.0) for _, _, earned in rows)
    realized_pnls = [pnl for _, pnl, _ in rows if pnl is not None]
    realized_pnl_usd = sum(realized_pnls) if realized_pnls else 0.0
    wins = sum(1 for pnl in realized_pnls if pnl > 0)
    win_rate_pct = (wins / len(realized_pnls) * 100.0) if realized_pnls else None

    # Unrealized P&L only makes sense for currently-open positions,
    # independent of the status filter above (a "confirmed only" financial
    # summary still wants to know what's sitting open right now).
    open_trades = _apply_common_filters(
        db.session.query(Trade).filter(Trade.user_id == user_id, Trade.executed == False),  # noqa: E712
        args, wallet_public_key,
    ).all()
    unrealized_pnl_usd = 0.0
    if open_trades:
        latest_prices = dict(
            db.session.query(_LATEST_PRICE_SUBQ.c.trade_id, _LATEST_PRICE_SUBQ.c.price)
            .filter(
                _LATEST_PRICE_SUBQ.c.trade_id.in_([t.id for t in open_trades]),
                _LATEST_PRICE_SUBQ.c.rn == 1,
            )
            .all()
        )
        for t in open_trades:
            current_price = latest_prices.get(t.id)
            if current_price is None or t.initial_price is None:
                continue
            unrealized_pnl_usd += (float(current_price) - t.initial_price) * (t.purchased_token_amount or 0.0)

    by_day_raw = _apply_status_filter(_apply_common_filters(
        db.session.query(
            func.date(Trade.created_at).label("day"),
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.amount).label("sol_spent"),
            func.sum(pnl_expr).label("realized_pnl_usd"),
            func.sum(_SELL_AGG_SUBQ.c.proceeds).label("earned_usd"),
        )
        .outerjoin(_SELL_AGG_SUBQ, _SELL_AGG_SUBQ.c.trade_id == Trade.id)
        .filter(Trade.user_id == user_id),
        args, wallet_public_key,
    ), status)
    by_day = [
        {
            "day": str(day),
            "trade_count": count,
            "sol_spent": float(spent or 0.0),
            "realized_pnl_usd": float(pnl or 0.0),
            "earned_usd": float(earned or 0.0),
        }
        for day, count, spent, pnl, earned in by_day_raw.group_by(func.date(Trade.created_at)).order_by("day").all()
    ]

    by_wallet_raw = _apply_status_filter(_apply_common_filters(
        db.session.query(
            Trade.to_pubkey,
            Wallet.title,
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.amount).label("sol_spent"),
            func.sum(pnl_expr).label("realized_pnl_usd"),
            func.sum(_SELL_AGG_SUBQ.c.proceeds).label("earned_usd"),
        )
        .outerjoin(_SELL_AGG_SUBQ, _SELL_AGG_SUBQ.c.trade_id == Trade.id)
        .outerjoin(Wallet, Wallet.public_key == Trade.to_pubkey)
        .filter(Trade.user_id == user_id),
        args, wallet_public_key,
    ), status)
    by_wallet = [
        {
            "to_pubkey": pubkey,
            "title": title,
            "trade_count": count,
            "sol_spent": float(spent or 0.0),
            "realized_pnl_usd": float(pnl or 0.0),
            "earned_usd": float(earned or 0.0),
        }
        for pubkey, title, count, spent, pnl, earned in by_wallet_raw.group_by(Trade.to_pubkey, Wallet.title).all()
    ]

    # Current SOL/USD rate, for the *approximate* SOL-equivalent figures
    # below only - cached process-wide already (shyft_pricing's own hot-path
    # cache for the sell bot), so this is not a new network call per
    # request. None on failure (Pyth read error) rather than raising - the
    # rest of the summary (all exact) must still return.
    try:
        sol_usd_rate = get_shyft_pricing_service().get_sol_usd()
    except Exception:
        sol_usd_rate = None

    def _approx_sol(usd_value):
        if sol_usd_rate is None or sol_usd_rate <= 0 or usd_value is None:
            return None
        return usd_value / sol_usd_rate

    def _approx_usd(sol_value):
        if sol_usd_rate is None or sol_value is None:
            return None
        return sol_value * sol_usd_rate

    return jsonify({
        "totals": {
            "sol_spent": sol_spent,
            # sol_spent is exact (it's the configured buy amount, actually
            # swapped in - see the module docstring). This USD figure is the
            # approximate part: sol_spent converted at today's rate, same as
            # every other *_approx field below - a sell made weeks ago in
            # the sol_spent total was at a different real SOL/USD rate.
            "sol_spent_usd_approx": _approx_usd(sol_spent),
            "earned_usd": earned_usd,
            "earned_sol_approx": _approx_sol(earned_usd),
            "sol_received": None,
            "not_tracked": True,
            "realized_pnl_sol": None,
            "realized_pnl_sol_approx": _approx_sol(realized_pnl_usd),
            "realized_pnl_usd": realized_pnl_usd,
            "unrealized_pnl_sol": None,
            "unrealized_pnl_sol_approx": _approx_sol(unrealized_pnl_usd),
            "unrealized_pnl_usd": unrealized_pnl_usd,
            "fees_paid_sol": None,
            "win_rate_pct": win_rate_pct,
            "trade_count": trade_count,
            "sol_usd_rate_used": sol_usd_rate,
        },
        "by_day": by_day,
        "by_wallet": by_wallet,
    })


@dashboard_api_bp.route("/api/dashboard/latency", methods=["GET"])
@jwt_required()
def latency_summary():
    """Latency isn't per-user (the sniper/sell cycle isn't scoped to one
    caller), so this just requires a valid token, not a per-row ownership
    filter - there's nothing here to own."""
    args = request.args
    query = LatencySnapshot.query
    path = args.get("path")
    if path:
        query = query.filter(LatencySnapshot.path == path)
    from_date = _parse_date(args.get("from"))
    if from_date:
        query = query.filter(LatencySnapshot.window_start >= from_date)
    to_date = _parse_date(args.get("to"))
    if to_date:
        query = query.filter(LatencySnapshot.window_end <= to_date)

    snapshots = query.order_by(LatencySnapshot.window_start.asc()).all()
    series = [
        {
            "window_start": s.window_start.isoformat(),
            "path": s.path,
            "stage": s.stage,
            "avg_ms": s.avg_ms,
            "max_ms": s.max_ms,
            "count": s.count,
        }
        for s in snapshots
    ]

    def _latest_total_avg(for_path):
        row = (
            LatencySnapshot.query
            .filter(LatencySnapshot.path == for_path, LatencySnapshot.stage == "total")
            .order_by(LatencySnapshot.window_start.desc())
            .first()
        )
        return row.avg_ms if row else None

    return jsonify({
        "series": series,
        "current": {
            "buy_total_avg_ms": _latest_total_avg("buy"),
            "sell_total_avg_ms": _latest_total_avg("sell"),
        },
    })
