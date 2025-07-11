import os
import time

from flask_jwt_extended import jwt_required, get_jwt_identity

from models import db, Wallet, Trade, TradeHistory
import requests
import base64
from flask import jsonify, request, Blueprint
from dotenv import load_dotenv
from requests import JSONDecodeError
from solana.rpc.core import RPCException
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from settings import solana_client
from utils import get_token_symbol_and_price, get_token_metadata, extract_token_info_from_moralis
from solders.pubkey import Pubkey

solana_bp = Blueprint('solana_bp', __name__)

load_dotenv()
API_KEY = os.getenv("API_KEY")
# Free tier users should use lite-api.jup.ag. api.jup.ag is for paid plans and requires an API key
API_BASE_URL = "https://api.jup.ag" if API_KEY else "https://lite-api.jup.ag"
# Set up headers for API requests (include x-api-key if API_KEY is available)
headers = {"x-api-key": API_KEY} if API_KEY else {}


@solana_bp.route('/trade', methods=['POST'])
@jwt_required()
def buy_token_trade():
    global signature
    try:
        user_id = get_jwt_identity()
        data = request.json
        to_pubkey = data.get("to_pubkey")
        token_address = data.get("token_address")
        amount = data.get("amount")
        current_token_price_ = get_token_symbol_and_price(token_address)
        current_token_price = current_token_price_["usdPrice"]
        token_name = current_token_price_["name"]
        token_symbol = current_token_price_["symbol"]
        sell_20_at_200_percent_profit = 0
        sell_10_at_300_percent_profit = 0
        sell_10_at_400_percent_profit = 0
        estimated_tokens = 0
        # ✅ Check if wallet exists
        sell_100_at_30_percent_drop = data.get("sell_100_at_30_percent_drop") if data.get(
            "sell_100_at_30_percent_drop") else 0.70
        sell_100_after_100_percent_profit_drop = data.get("sell_100_after_100_percent_profit_drop") if data.get(
            "sell_100_after_100_percent_profit_drop") else 30.00
        sell_at_200_percent_profit = data.get("sell_at_200_percent_profit") if data.get(
            "sell_at_200_percent_profit") else 30.00
        sell_at_300_percent_profit = data.get("sell_at_300_percent_profit") if data.get(
            "sell_at_300_percent_profit") else 30.00
        sell_at_500_percent_profit = data.get("sell_at_500_percent_profit") if data.get(
            "sell_at_500_percent_profit") else 10.00
        sell_at_1000_percent_profit = data.get("sell_at_1000_percent_profit") if data.get(
            "sell_at_1000_percent_profit") else 10.00
        sell_at_2000_percent_profit = data.get("sell_at_2000_percent_profit") if data.get(
            "sell_at_2000_percent_profit") else 10.00
        sell_at_10000_percent_profit = data.get("sell_at_10000_percent_profit") if data.get(
            "sell_at_10000_percent_profit") else 100.00
        long_slippage = data.get("buy_slippage") if data.get("buy_slippage") else 0.10
        default_gas_fee = data.get("buy_gas_fee") if data.get("buy_gas_fee") else 0.01
        buy_now = data.get("buy_now") if data.get("buy_now") else False
        buy_token_if_price = data.get("buy_token_if_price") if data.get("buy_token_if_price") else False
        buy_if_price_up = data.get("buy_if_price_up") if data.get("buy_if_price_up") else 0
        buy_if_price_down = data.get("buy_if_price_down") if data.get("buy_if_price_down") else 0
        gas_fee = default_gas_fee if default_gas_fee else 0
        long_sell_slippage = data.get("sell_slippage") if data.get("sell_slippage") else 0.10
        long_sell_gas_fee = data.get("sell_gas_fee") if data.get("sell_gas_fee") else 0.01
        if buy_now is True:

            wallet = Wallet.query.filter_by(public_key=to_pubkey).first()
            if not wallet:
                return jsonify({"status": "failed", "message": "Failed to fetch wallet"}), 400

            wallet_balance_response = solana_client.get_balance(Pubkey.from_string(to_pubkey))
            wallet_balance = wallet_balance_response.value / 1_000_000_000  # Convert lamports to SOL

            # ✅ Check if balance is sufficient
            if wallet_balance < amount:
                return jsonify({
                    "status": "failed",
                    "message": f"Insufficient balance. Wallet has {wallet_balance:.4f} SOL, but {amount:.4f} SOL is required."
                }), 400

            private_key_path = wallet.private_key
            if not os.path.exists(private_key_path):
                return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

            with open(private_key_path, 'rb') as key_file:
                private_key_bytes = key_file.read()
                # Create a solders Keypair directly from the private key bytes
            wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)

            # Fetch a quote to swap WSOL (Wrapped SOL) to USDC tokens
            amount_in_lamports = int(amount * 1_000_000_000)

            quote_params = {
                "inputMint": "So11111111111111111111111111111111111111112",  # WSOL
                "outputMint": token_address,  # USDC
                "amount": amount_in_lamports,  # 0.01 WSOL
                "slippageBps": long_slippage
            }

            quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
            quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)

            if quote_response.status_code != 200:
                try:
                    print(f"Error fetching quote: {quote_response.json()}")
                    return jsonify({"status": "failed", "message": "Error fetching quote"}), 400
                except JSONDecodeError as e:
                    print(f"Error fetching quote: {quote_response.json()}")
                    return jsonify({"status": "failed", "message": "Error fetching quote"}), 400
                # finally:
                #     exit()

            quote_data = quote_response.json()

            # Fetch the swap transaction for the quote
            swap_request = {
                "userPublicKey": str(wallet_keypair.pubkey()),
                "quoteResponse": quote_data,
                "computeUnitPriceMicroLamports": int(gas_fee * 1_000_000),  # Convert SOL to micro-lamports
                "wrapUnwrapSOL": True  # Automatically handle SOL to WSOL conversion
            }

            swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
            swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers)

            if swap_response.status_code != 200:
                try:
                    print(f"Error performing swap: {swap_response.json()}")
                    return jsonify({"status": "failed", "message": "Error performing swap"}), 400
                except JSONDecodeError as e:
                    print(f"Error performing swap: {swap_response.json()}")
                    return jsonify({"status": "failed", "message": "Error performing swap"}), 400
                # finally:
                #     exit()

            swap_data = swap_response.json()

            # Get Raw Transaction
            swap_transaction_base64 = swap_data["swapTransaction"]
            swap_transaction_bytes = base64.b64decode(swap_transaction_base64)
            raw_transaction = VersionedTransaction.from_bytes(swap_transaction_bytes)

            # Sign Transaction
            account_keys = raw_transaction.message.account_keys
            wallet_index = account_keys.index(wallet_keypair.pubkey())

            signers = list(raw_transaction.signatures)
            signers[wallet_index] = wallet_keypair

            signed_transaction = VersionedTransaction(raw_transaction.message, signers)

            # Send the signed transaction to the RPC client
            try:
                start_time = time.time()
                rpc_response = solana_client.send_transaction(signed_transaction)
                end_time = time.time()
                execution_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds
                signature = str(rpc_response.value)
                print(f"✅ Auto trade job executed in {execution_time_ms:.2f} ms!")
                print(f"Transaction sent successfully! Signature: {signature}")
                print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
                estimated_tokens = int(quote_data.get("outAmount", 0)) / (10 ** quote_data.get("outputMintDecimals", 6))

                # Calculate estimated tokens received
                # output_amount = int(quote_data.get("outAmount", 0))
                # metadata = get_token_metadata(token_address)
                # if metadata:
                #     token_decimals = metadata.get("decimals", 0)
                #     print(f"Token has {token_decimals} decimals")
                # else:
                #     token_decimals = 0
                # estimated_tokens = output_amount / (10 ** token_decimals)
            except RPCException as e:
                error_message = e.args[0]
                print("Transaction failed!")
                print(f"Custom Program Error Code: {error_message.data.err.err.code}")
                print(f"Message: {error_message.message}")
                return jsonify({"status": "failed", "message": error_message}), 400
        if buy_now is True:
            trade_type = "SELL"
        else:
            trade_type = "BUY"
        new_trade = Trade(
            to_pubkey=to_pubkey,
            amount=amount,
            trade_type=trade_type,
            initial_price=current_token_price,
            token_address=token_address,
            token_name=token_name,
            token_symbol=token_symbol,
            trade_kind="LONG",
            sell_100_at_30_percent_drop=sell_100_at_30_percent_drop if sell_100_at_30_percent_drop else 0,
            sell_20_at_200_percent_profit=sell_20_at_200_percent_profit if sell_20_at_200_percent_profit else 0,
            sell_10_at_300_percent_profit=sell_10_at_300_percent_profit if sell_10_at_300_percent_profit else 0,
            sell_10_at_400_percent_profit=sell_10_at_400_percent_profit if sell_10_at_400_percent_profit else 0,
            sell_100_after_100_percent_profit_drop=sell_100_after_100_percent_profit_drop if sell_100_after_100_percent_profit_drop else 0,
            sell_at_200_percent_profit=sell_at_200_percent_profit if sell_at_200_percent_profit else 0,
            sell_at_300_percent_profit=sell_at_300_percent_profit if sell_at_300_percent_profit else 0,
            sell_at_500_percent_profit=sell_at_500_percent_profit if sell_at_500_percent_profit else 0,
            sell_at_1000_percent_profit=sell_at_1000_percent_profit if sell_at_1000_percent_profit else 0,
            sell_at_2000_percent_profit=sell_at_2000_percent_profit if sell_at_2000_percent_profit else 0,
            sell_at_10000_percent_profit=sell_at_10000_percent_profit if sell_at_10000_percent_profit else 0,
            default_gas_fee=default_gas_fee if default_gas_fee else 0,
            long_sell_gas_fee=long_sell_gas_fee if long_sell_gas_fee else 0,
            long_slippage=long_slippage if long_slippage else 0,
            long_sell_slippage=long_sell_slippage if long_sell_slippage else 0,
            purchased_token_amount=estimated_tokens if estimated_tokens else 0,
            buy_now=buy_now if buy_now else False,
            buy_token_if_price=buy_token_if_price if buy_token_if_price else False,
            buy_if_price_up=buy_if_price_up if buy_if_price_up else 0,
            buy_if_price_down=buy_if_price_down if buy_if_price_down else 0,
            config_id=None,  # ✅ Store selected configuration
            user_id=int(user_id),

        )
        db.session.add(new_trade)
        db.session.commit()
        if buy_now is True:
            executed_trade = TradeHistory(
                trade_id=new_trade.id if new_trade else None,
                token_address=token_address,
                trade_type="BUY",
                trade_kind="LONG",
                amount=amount,
                execution_price=current_token_price,  # Use the fetched price
                tx_id=signature
            )
            db.session.add(executed_trade)
            db.session.commit()
        return jsonify({"status": "success", "message": "Trade run successfully"}), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 500


"""
fetch all trade
"""


@solana_bp.route('/get_trades', methods=['GET'])
@jwt_required()
def get_trades():
    """Fetch all trades and return only the trade array based on configuration type."""
    try:
        user_id = get_jwt_identity()
        trades = Trade.query.filter_by(user_id=user_id).order_by(Trade.created_at.desc()).all()
        # trades = Trade.query.order_by(Trade.created_at.desc()).all()
        trade_list = []

        for trade in trades:
            base_trade_data = {
                "id": trade.id,
                "to_pubkey": trade.to_pubkey,
                "amount": trade.amount,
                "trade_type": trade.trade_type,
                "initial_price": trade.initial_price,
                "token_address": trade.token_address,
                "purchase_token": trade.purchased_token_amount if trade.purchased_token_amount else 0,
                "title": trade.token_name,
                "token_symbol": trade.token_symbol,
                "created_at": trade.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            }

            # LONG Auto-sell conditions
            base_trade_data.update({
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
            })

            trade_list.append(base_trade_data)

        return jsonify(trade_list), 200  # Return only the filtered array

    except Exception as e:
        return jsonify({"error": str(e)}), 500


"""
update trade api
"""


@solana_bp.route('/trade/<int:trade_id>/', methods=['PUT'])
def update_trade(trade_id):
    """Update an existing trade's conditions."""
    try:
        data = request.json
        if not trade_id:
            return jsonify({"status": "failed", "message": "Missing trade_id"}), 400

        # Fetch trade from DB
        trade = Trade.query.filter_by(id=trade_id).first()
        if not trade:
            return jsonify({"status": "failed", "message": "Trade not found"}), 404

        # Mapping payload fields to database fields
        field_mapping = {
            "buy_gas_fee": "default_gas_fee",
            "buy_slippage": "long_slippage",
            "sell_gas_fee": "long_sell_gas_fee",
            "sell_slippage": "long_sell_slippage",
            "sell_100_after_100_percent_profit_drop": "sell_100_after_100_percent_profit_drop",
            "sell_100_at_30_percent_drop": "sell_100_at_30_percent_drop",
            "sell_at_200_percent_profit": "sell_at_200_percent_profit",
            "sell_at_300_percent_profit": "sell_at_300_percent_profit",
            "sell_at_500_percent_profit": "sell_at_500_percent_profit",
            "sell_at_1000_percent_profit": "sell_at_1000_percent_profit",
            "sell_at_2000_percent_profit": "sell_at_2000_percent_profit",
            "sell_at_10000_percent_profit": "sell_at_10000_percent_profit",
        }

        # Apply updates based on mapping
        for payload_field, db_field in field_mapping.items():
            if payload_field in data:
                setattr(trade, db_field, data[payload_field])

        db.session.commit()

        return jsonify({"status": "success", "message": "Trade updated successfully", "trade_id": trade.id})

    except Exception as e:
        return jsonify({"status": "failed", "message": str(e)}), 500


# get specific trade
@solana_bp.route('/token_stats/<int:trade_id>', methods=['GET'])
def token_stats(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    token_address = trade.token_address

    moralis_data = get_token_symbol_and_price(token_address)  # Your current function
    if not moralis_data:
        return jsonify({"error": "Failed to fetch token data"}), 400

    result = extract_token_info_from_moralis(moralis_data, trade)
    if not result:
        return jsonify({"error": "Invalid token data"}), 400

    return jsonify(result)


# buy more token against this trade
@solana_bp.route('/buy_token/<int:trade_id>/', methods=['PUT'])
def buy_token(trade_id):
    global signature
    try:
        data = request.json
        trade = Trade.query.get(trade_id)
        to_pubkey = trade.to_pubkey if trade.to_pubkey else ""
        token_address = trade.token_address if trade.token_address else ""
        amount = data.get("amount")
        current_token_price_ = get_token_symbol_and_price(token_address)
        current_token_price = current_token_price_["usdPrice"]
        wallet = Wallet.query.filter_by(public_key=to_pubkey).first()
        if not wallet:
            return jsonify({"status": "failed", "message": "Failed to fetch wallet"}), 400

        wallet_balance_response = solana_client.get_balance(Pubkey.from_string(to_pubkey))
        wallet_balance = wallet_balance_response.value / 1_000_000_000  # Convert lamports to SOL

        # ✅ Check if balance is sufficient
        if wallet_balance < amount:
            return jsonify({
                "status": "failed",
                "message": f"Insufficient balance. Wallet has {wallet_balance:.4f} SOL, but {amount:.4f} SOL is required."
            }), 400

        private_key_path = wallet.private_key
        if not os.path.exists(private_key_path):
            return jsonify({"status": "failed", "message": "Failed to fetch private key"}), 400

        with open(private_key_path, 'rb') as key_file:
            private_key_bytes = key_file.read()
            # Create a solders Keypair directly from the private key bytes
        wallet_keypair = SoldersKeypair.from_seed(private_key_bytes)

        # Fetch a quote to swap WSOL (Wrapped SOL) to USDC tokens
        amount_in_lamports = int(amount * 1_000_000_000)

        quote_params = {
            "inputMint": "So11111111111111111111111111111111111111112",  # WSOL
            "outputMint": token_address,  # USDC
            "amount": amount_in_lamports,  # 0.01 WSOL
            "slippageBps": int(trade.long_slippage)
        }

        quote_endpoint = f"{API_BASE_URL}/swap/v1/quote"
        quote_response = requests.get(quote_endpoint, params=quote_params, headers=headers)

        if quote_response.status_code != 200:
            try:
                print(f"Error fetching quote: {quote_response.json()}")
                return jsonify({"status": "failed", "message": "Error fetching quote"}), 400
            except JSONDecodeError as e:
                print(f"Error fetching quote: {quote_response.json()}")
                return jsonify({"status": "failed", "message": "Error fetching quote"}), 400
            # finally:
            #     exit()

        quote_data = quote_response.json()

        # Fetch the swap transaction for the quote
        swap_request = {
            "userPublicKey": str(wallet_keypair.pubkey()),
            "quoteResponse": quote_data,
            "computeUnitPriceMicroLamports": int(trade.default_gas_fee * 1_000_000),  # Convert SOL to micro-lamports
            "wrapUnwrapSOL": True  # Automatically handle SOL to WSOL conversion
        }

        swap_endpoint = f"{API_BASE_URL}/swap/v1/swap"
        swap_response = requests.post(swap_endpoint, json=swap_request, headers=headers)

        if swap_response.status_code != 200:
            try:
                print(f"Error performing swap: {swap_response.json()}")
                return jsonify({"status": "failed", "message": "Error performing swap"}), 400
            except JSONDecodeError as e:
                print(f"Error performing swap: {swap_response.json()}")
                return jsonify({"status": "failed", "message": "Error performing swap"}), 400
            # finally:
            #     exit()

        swap_data = swap_response.json()

        # Get Raw Transaction
        swap_transaction_base64 = swap_data["swapTransaction"]
        swap_transaction_bytes = base64.b64decode(swap_transaction_base64)
        raw_transaction = VersionedTransaction.from_bytes(swap_transaction_bytes)

        # Sign Transaction
        account_keys = raw_transaction.message.account_keys
        wallet_index = account_keys.index(wallet_keypair.pubkey())

        signers = list(raw_transaction.signatures)
        signers[wallet_index] = wallet_keypair

        signed_transaction = VersionedTransaction(raw_transaction.message, signers)

        # Send the signed transaction to the RPC client
        try:
            start_time = time.time()
            rpc_response = solana_client.send_transaction(signed_transaction)
            end_time = time.time()
            execution_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds
            signature = str(rpc_response.value)
            print(f"✅ Auto trade job executed in {execution_time_ms:.2f} ms!")
            print(f"Transaction sent successfully! Signature: {signature}")
            print(f"View transaction on Solscan: https://solscan.io/tx/{signature}")
            estimated_tokens = int(quote_data.get("outAmount", 0)) / (10 ** quote_data.get("outputMintDecimals", 6))
            trade.purchased_token_amount += estimated_tokens
            trade.amount += amount  # Soft delete instead of actual deletion
            if trade.executed is True:
                trade.executed = False
            db.session.commit()
        except RPCException as e:
            error_message = e.args[0]
            print("Transaction failed!")
            print(f"Custom Program Error Code: {error_message.data.err.err.code}")
            print(f"Message: {error_message.message}")
            return jsonify({"status": "failed", "message": error_message}), 400
        executed_trade = TradeHistory(
            trade_id=trade.id if trade else None,
            token_address=token_address,
            trade_type="BUY",
            amount=amount,
            execution_price=current_token_price,  # Use the fetched price
            tx_id=signature
        )
        db.session.add(executed_trade)
        db.session.commit()
        return jsonify({"status": "success", "message": "Trade run successfully"}), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 500


@solana_bp.route('/api/update_auto_sell/<int:trade_id>', methods=['POST'])
def update_auto_sell(trade_id):
    try:
        data = request.get_json()
        auto_sell = data.get('auto_sell')

        if auto_sell is None:
            return jsonify({"status": "error", "message": "Missing 'auto_sell' field"}), 400

        trade = Trade.query.get(trade_id)
        if not trade:
            return jsonify({"status": "error", "message": "Trade not found"}), 404

        trade.auto_sell = bool(auto_sell)
        db.session.commit()

        return jsonify({"message": "auto sell update successfully", "auto_sell": trade.auto_sell}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500