import os
import time
from models import db, Wallet, Trade, TradeHistory
import requests
import base64
from flask import Flask, jsonify, request, Blueprint
from dotenv import load_dotenv
from requests import JSONDecodeError
from solana.rpc.core import RPCException
from solders.solders import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair
from solders.pubkey import Pubkey
from settings import solana_client
from utils import get_token_symbol_and_price, get_token_metadata, extract_token_info_from_moralis
# from solana.publickey import PublicKey
from solders.pubkey import Pubkey

import threading
from datetime import datetime

solana_auto_snipe_bp = Blueprint('solana_auto_snipe_bp', __name__)

load_dotenv()
API_KEY = os.getenv("API_KEY")
API_BASE_URL = "https://api.jup.ag" if API_KEY else "https://lite-api.jup.ag"
headers = {"x-api-key": API_KEY} if API_KEY else {}

# Global state for sniping
transaction_history = {}
launch_times = {}


@solana_auto_snipe_bp.route('/trade', methods=['POST'])
def buy_token_trade():
    pass
    # Your existing buy_token_trade function (as provided)
    # ... (keep your existing code here) ...


# Sniping functions
def get_new_token_mints(solana_client):
    token_program_id = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    accounts = solana_client.get_program_accounts(token_program_id, encoding="base64")
    return [str(account["pubkey"]) for account in accounts.value if is_recently_created(account)]


def is_recently_created(account):
    # Placeholder: Implement logic to check creation time
    return True


def monitor_token_activity(token_mint_address, solana_client):
    global transaction_history, launch_times
    while True:
        current_time = datetime.now().timestamp()
        signatures = solana_client.get_signatures_for_address(Pubkey.from_string(token_mint_address), limit=10)
        for sig in signatures.value:
            tx = solana_client.get_transaction(sig.signature)
            amount = extract_transaction_amount(tx)
            if amount > 80:
                if token_mint_address not in transaction_history:
                    transaction_history[token_mint_address] = []
                transaction_history[token_mint_address].append((current_time, amount))
                transaction_history[token_mint_address] = [t for t in transaction_history[token_mint_address] if
                                                           current_time - t[0] <= 5]

                if token_mint_address not in launch_times and transaction_history[token_mint_address]:
                    launch_times[token_mint_address] = current_time

                if (len(transaction_history[token_mint_address]) >= 5 and current_time - launch_times[
                    token_mint_address] <= 5):
                    execute_snipe_buy(token_mint_address)
                    del transaction_history[token_mint_address]
                    del launch_times[token_mint_address]
                    break
        time.sleep(0.5)


def extract_transaction_amount(tx):
    # Placeholder: Use Jupiter API or tx data to estimate USD
    return 100  # Replace with actual logic


def execute_snipe_buy(token_mint_address):
    buy_amount_sol = 1.0
    slippage = 100
    priority_fee = 0.01
    to_pubkey = str(wallet_keypair.pubkey())  # Use your wallet

    payload = {
        "to_pubkey": to_pubkey,
        "token_address": token_mint_address,
        "amount": buy_amount_sol,
        "buy_slippage": slippage,
        "buy_gas_fee": priority_fee,
        "buy_now": True,
        "sell_100_at_30_percent_drop": 0.70,
        "sell_100_after_100_percent_profit_drop": 30.00,
        "sell_at_200_percent_profit": 30.00,
        "sell_at_300_percent_profit": 30.00,
        "sell_at_500_percent_profit": 10.00,
        "sell_at_1000_percent_profit": 10.00,
        "sell_at_2000_percent_profit": 10.00,
        "sell_at_10000_percent_profit": 100.00
    }

    try:
        response = requests.post(
            "http://localhost:5000/solana/trade",
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        if response.status_code == 200:
            result = response.json()
            print(f"Snipe buy successful: {result}")
        else:
            print(f"Snipe buy failed: {response.text}")
    except Exception as e:
        print(f"Snipe buy error: {e}")


@solana_auto_snipe_bp.route('/auto-snipe', methods=['GET'])
def start_auto_snipe():
    # with app.app_context():
        while True:
            new_tokens = get_new_token_mints(solana_client)
            for token_mint_address in new_tokens:
                if token_mint_address not in transaction_history:
                    thread = threading.Thread(target=monitor_token_activity, args=(token_mint_address, solana_client))
                    thread.daemon = True
                    thread.start()
            # time.sleep(5)


# Load wallet (assuming it's from your existing setup)
wallet = Wallet.query.filter_by(public_key="your_public_key").first()
wallet_keypair = SoldersKeypair.from_seed(open(wallet.private_key, 'rb').read())  # Adjust path

snipe_thread = threading.Thread(target=start_auto_snipe, args=(solana_client,))
snipe_thread.daemon = True
snipe_thread.start()
