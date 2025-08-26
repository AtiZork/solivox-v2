from models import Trade, TokenPrice
from main import socketio, app
import time
from threading import Thread
from flask import request
from solders.pubkey import Pubkey
from settings import solana_client
from utils import get_token_symbol_and_price, calculate_profit_and_payout, get_estimated_market_cap
from flask_socketio import emit, disconnect
from flask import copy_current_request_context


# @socketio.on("subscribe_trades")
# def handle_trade_subscription():
#     sid = request.sid  # Get the session ID of the connected client
#
#     @copy_current_request_context
#     def emit_trade_loop():
#         global market_cap
#         # while True:
#         try:
#             # trades = Trade.query.order_by(Trade.created_at.desc()).all()
#             trades = Trade.query.order_by(Trade.created_at.desc()).limit(1).all()
#             trade_data = []
#
#             for trade in trades:
#                 price_info = get_token_symbol_and_price(trade.token_address)
#                 if price_info:
#                     current_price = price_info.get("usdPrice", 0)
#                 else:
#                     current_price = 0
#                 # fetch market cap
#                 try:
#                     pubkey = Pubkey.from_string(trade.token_address)
#                     response = solana_client.get_token_supply(pubkey)
#                     if response.value:
#                         supply = int(response.value.amount)
#                         decimals = int(response.value.decimals)
#                         adjusted_supply = supply / (10 ** decimals)
#                         market_cap = adjusted_supply * current_price
#                 except Exception as e:
#                     print(f"[MarketCap] Solana RPC fallback failed: {e}")
#                     market_cap = 0
#                 sol_price_data_ = get_token_symbol_and_price("So11111111111111111111111111111111111111112")  # WSOL
#                 if sol_price_data_:
#                     sol_price_data = float(sol_price_data_.get("usdPrice", 0))
#
#                     profit, payout_usd, payout_sol = calculate_profit_and_payout(trade, current_price, sol_price_data)
#                 else:
#                     profit = 0
#                     payout_usd = 0
#                     payout_sol = 0
#
#                 trade_data.append({
#                     "id": trade.id,
#                     "token_name": trade.token_name if trade.token_name else trade.token_address,
#                     "token_address": trade.token_address if trade.token_address else "",
#                     "symbol": trade.token_symbol,
#                     "initial_price": float(trade.initial_price if trade.initial_price else 0),
#                     "current_price": f"{current_price:.6f}$",
#                     "profit": f"{profit:.2f}%",
#                     "payout_usd": f"{payout_usd:.4f} USD",
#                     "payout_sol": f"{payout_sol:.4f} SOL",
#                     "market_cap": market_cap,
#                     "sell_100_at_30_percent_drop": float(trade.sell_100_at_30_percent_drop),
#                     "sell_100_after_100_percent_profit_drop": float(trade.sell_100_after_100_percent_profit_drop),
#                     "sell_at_200_percent_profit": float(trade.sell_at_200_percent_profit),
#                     "sell_at_300_percent_profit": float(trade.sell_at_300_percent_profit),
#                     "sell_at_500_percent_profit": float(trade.sell_at_500_percent_profit),
#                     "sell_at_1000_percent_profit": float(trade.sell_at_1000_percent_profit),
#                     "sell_at_2000_percent_profit": float(trade.sell_at_2000_percent_profit),
#                     "sell_at_10000_percent_profit": float(trade.sell_at_10000_percent_profit),
#                     "buy_slippage": float(trade.long_slippage),
#                     "sell_slippage": float(trade.long_sell_slippage),
#                     "sell_gas_fee": float(trade.long_sell_gas_fee),
#                     "buy_gas_fee": float(trade.long_buy_gas_fee),
#                     "buy_if_price_down": float(trade.buy_if_price_down if trade.buy_if_price_down else 0),
#                     "buy_if_price_up": float(trade.buy_if_price_up if trade.buy_if_price_up else 0),
#                     "amount": float(trade.amount),
#                     "buy_now": trade.buy_now,
#                     "created_at": trade.created_at.strftime("%Y-%m-%d %H:%M:%S")
#                 })
#
#             emit("trades_data", trade_data, to=sid)
#             print(trade_data)
#             # time.sleep(6000)
#
#         except Exception as e:
#             print(f"[Trade Emit Error] {e}")
#             # break
#
#     # ✅ Start background thread correctly
#     Thread(target=emit_trade_loop).start()

@socketio.on("subscribe_trades")
def handle_trade_subscription():

    sid = request.sid  # Get the session ID of the connected client
    global market_cap
    # while True:
    try:
        # trades = Trade.query.order_by(Trade.created_at.desc()).all()
        trades = Trade.query.order_by(Trade.created_at.desc()).limit(4).all()
        trade_data = []

        for trade in trades:
            price_info = get_token_symbol_and_price(trade.token_address)
            if price_info:
                current_price = price_info.get("usdPrice", 0)
            else:
                current_price = 0
            # fetch market cap
            try:
                pubkey = Pubkey.from_string(trade.token_address)
                response = solana_client.get_token_supply(pubkey)
                if response.value:
                    supply = int(response.value.amount)
                    decimals = int(response.value.decimals)
                    adjusted_supply = supply / (10 ** decimals)
                    market_cap = adjusted_supply * current_price
            except Exception as e:
                print(f"[MarketCap] Solana RPC fallback failed: {e}")
                market_cap = 0
            sol_price_data_ = get_token_symbol_and_price("So11111111111111111111111111111111111111112")  # WSOL
            if sol_price_data_:
                sol_price_data = float(sol_price_data_.get("usdPrice", 0))

                profit, payout_usd, payout_sol = calculate_profit_and_payout(trade, current_price, sol_price_data)
            else:
                profit = 0
                payout_usd = 0
                payout_sol = 0

            # --- Get token price history for chart ---
            # token_prices = TokenPrice.query.filter_by(trade_id=trade.id).order_by(TokenPrice.timestamp).limit(30).all()
            token_prices = TokenPrice.query.filter_by(trade_id=trade.id).order_by(TokenPrice.timestamp.desc()).limit(30).all()
            token_prices = list(reversed(token_prices))
            price_points = [
                {
                    "id": p.id,
                    # "price": float(p.price),
                    "price": format(float(p.price), ".8f"),
                    "timestamp": p.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                } for p in token_prices
            ]
            print(price_points)
            trade_data.append({
                "id": trade.id,
                "token_name": trade.token_name if trade.token_name else trade.token_address,
                "token_address": trade.token_address if trade.token_address else "",
                "symbol": trade.token_symbol,
                "initial_price": float(trade.initial_price if trade.initial_price else 0),
                "current_price": f"{current_price:.6f}$",
                "profit": f"{profit:.2f}%",
                "payout_usd": f"{payout_usd:.4f} USD",
                "payout_sol": f"{payout_sol:.4f} SOL",
                "market_cap": market_cap,
                "prices": price_points,  # 🟢 CHART DATA HERE
                "sell_100_at_30_percent_drop": float(trade.sell_100_at_30_percent_drop),
                "sell_100_after_100_percent_profit_drop": float(trade.sell_100_after_100_percent_profit_drop),
                "sell_at_200_percent_profit": float(trade.sell_at_200_percent_profit),
                "sell_at_300_percent_profit": float(trade.sell_at_300_percent_profit),
                "sell_at_500_percent_profit": float(trade.sell_at_500_percent_profit),
                "sell_at_1000_percent_profit": float(trade.sell_at_1000_percent_profit),
                "sell_at_2000_percent_profit": float(trade.sell_at_2000_percent_profit),
                "sell_at_10000_percent_profit": float(trade.sell_at_10000_percent_profit),
                "buy_slippage": float(trade.long_slippage),
                "sell_slippage": float(trade.long_sell_slippage),
                "sell_gas_fee": float(trade.long_sell_gas_fee),
                "buy_gas_fee": float(trade.long_buy_gas_fee),
                "buy_if_price_down": float(trade.buy_if_price_down if trade.buy_if_price_down else 0),
                "buy_if_price_up": float(trade.buy_if_price_up if trade.buy_if_price_up else 0),
                "amount": float(trade.amount),
                "buy_now": trade.buy_now,
                "trade_type": trade.trade_type,
                "buy_token_if_price": trade.buy_token_if_price,
                "created_at": trade.created_at.strftime("%Y-%m-%d %H:%M:%S")
            })

        emit("trades_data", trade_data, to=sid)
        print(trade_data)
        # time.sleep(6000)

    except Exception as e:
        print(f"[Trade Emit Error] {e}")
        # break


# @socketio.on("subscribe_trades")
# def handle_trade_subscription():
#     sid = request.sid  # Get session ID of the connected client
#
#     @copy_current_request_context
#     def emit_trades_loop():
#         while True:
#             try:
#                 trades = Trade.query.order_by(Trade.created_at.desc()).all()
#                 trade_data = []
#
#                 for trade in trades:
#                     price_info = get_token_symbol_and_price(trade.token_address)
#                     current_price = price_info.get("usdPrice", 0)
#                     sol_price_data_ = get_token_symbol_and_price("So11111111111111111111111111111111111111112")  # WSOL mint
#                     sol_price_data = float(sol_price_data_.get("usdPrice", 0))
#                     profit, payout_usd, payout_sol = calculate_profit_and_payout(trade, current_price, sol_price_data)
#
#                     trade_data.append({
#                         "id": trade.id,
#                         "token": trade.token_name,
#                         "symbol": trade.token_symbol,
#                         "amount": trade.amount,
#                         "initial_price": trade.initial_price,
#                         "current_price": f"{current_price:.6f}$",
#                         "profit": f"{profit:.2f}%",
#                         "payout_usd": f"{payout_usd:.4f} USD",
#                         "payout_sol": f"{payout_sol:.4f} SOL",
#                         "sell_100_at_30_percent_drop": trade.sell_100_at_30_percent_drop,
#                         "sell_100_after_100_percent_profit_drop": trade.sell_100_after_100_percent_profit_drop,
#                         "sell_at_200_percent_profit": trade.sell_at_200_percent_profit,
#                         "sell_at_300_percent_profit": trade.sell_at_300_percent_profit,
#                         "sell_at_500_percent_profit": trade.sell_at_500_percent_profit,
#                         "sell_at_1000_percent_profit": trade.sell_at_1000_percent_profit,
#                         "sell_at_2000_percent_profit": trade.sell_at_2000_percent_profit,
#                         "sell_at_10000_percent_profit": trade.sell_at_10000_percent_profit,
#                         "buy_if_price_up": trade.buy_if_price_up,
#                         "buy_if_price_down": trade.buy_if_price_down,
#                         "buy_slippage": trade.long_slippage,
#                         "sell_slippage": trade.long_sell_slippage,
#                         "buy_gas_fee": trade.long_buy_gas_fee,
#                         "sell_gas_fee": trade.long_sell_gas_fee,
#                         "buy_now": trade.buy_now,
#                         "created_at": trade.created_at.strftime("%Y-%m-%d %H:%M:%S")
#                     })
#
#                 emit("trades_data", trade_data, to=sid)
#
#                 time.sleep(5)  # Wait 5 seconds before next update
#             except Exception as e:
#                 print(f"Trade emit error: {e}")
#                 disconnect(sid)
#                 break
#
#     # Start the background emitter
#     Thread(target=emit_trades_loop).start()


def push_trades():
    while True:
        with app.app_context():
            trades = Trade.query.order_by(Trade.created_at.desc()).all()
            trade_data = []

            for trade in trades:
                price_info = get_token_symbol_and_price(trade.token_address)
                current_price = price_info.get("usdPrice", 0)
                sol_price_data_ = get_token_symbol_and_price("So11111111111111111111111111111111111111112")  # WSOL mint
                sol_price_data = float(sol_price_data_.get("usdPrice", 0))
                profit, payout_usd, payout_sol = calculate_profit_and_payout(trade, current_price, sol_price_data)
                trade_data.append({
                    "id": trade.id,
                    "token": trade.token_name,
                    "symbol": trade.token_symbol,
                    "amount": trade.amount,
                    "initial_price": trade.initial_price,
                    "current_price": f"{current_price:.6f}$",
                    "profit": f"{profit:.2f}%",
                    "payout_usd": f"{payout_usd:.4f} SOL",
                    "payout_sol": f"{payout_sol:.4f} USD",
                    "sell_100_at_30_percent_drop": trade.sell_100_at_30_percent_drop,
                    "sell_100_after_100_percent_profit_drop": trade.sell_100_after_100_percent_profit_drop,
                    "sell_at_200_percent_profit": trade.sell_at_200_percent_profit,
                    "sell_at_300_percent_profit": trade.sell_at_300_percent_profit,
                    "sell_at_500_percent_profit": trade.sell_at_500_percent_profit,
                    "sell_at_1000_percent_profit": trade.sell_at_1000_percent_profit,
                    "sell_at_2000_percent_profit": trade.sell_at_2000_percent_profit,
                    "sell_at_10000_percent_profit": trade.sell_at_10000_percent_profit,
                    "buy_if_price_up": trade.buy_if_price_up,
                    "buy_if_price_down": trade.buy_if_price_down,
                    "buy_slippage": trade.long_slippage,
                    "sell_slippage": trade.long_sell_slippage,
                    "buy_gas_fee": trade.long_buy_gas_fee,
                    "sell_gas_fee": trade.long_sell_gas_fee,
                    "buy_now": trade.buy_now,
                    "created_at": trade.created_at.strftime("%Y-%m-%d %H:%M:%S")
                })

            socketio.emit("trades_data", trade_data)
        time.sleep(10)  # every second

# Start thread
# Thread(target=push_trades).start()
