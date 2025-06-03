from moralis import sol_api
from settings import moraliz_api_key, solana_client
import requests
from solders.pubkey import Pubkey


def get_token_symbol_and_price(token_mint: str):
    try:
        params = {
            "network": "mainnet",
            "address": token_mint
        }

        result = sol_api.token.get_token_price(
            api_key=moraliz_api_key,
            params=params,
        )
        return result
    except Exception as e:
        return None


# fetch token decimals on solana chain
def get_token_metadata(token_address):
    url = f"https://lite-api.jup.ag/tokens/v1/token/{token_address}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        token_data = response.json()
        return token_data
    except Exception as e:
        print(f"Error fetching token metadata for {token_address}: {e}")
        return None


def extract_token_info_from_moralis(moralis_result, trade):
    try:

        name = moralis_result.get("name")
        address = moralis_result.get("tokenAddress")
        price = float(moralis_result.get("usdPrice", 0))
        sol_price_data_ = get_token_symbol_and_price("So11111111111111111111111111111111111111112")  # WSOL mint
        sol_price_data = float(sol_price_data_.get("usdPrice", 0))

        # Custom logic for fields not in API
        profit, payout_usd, payout_sol = calculate_profit_and_payout(trade, price, sol_price_data)
        return {
            "name": name,
            "address": address,
            "price": f"${price:.6f}",  # e.g. 0.002300 becomes "$0.002300"
            "market_cap": get_estimated_market_cap(address),  # Still a plain float or None
            "profit": f"{profit:.2f}%",  # e.g. 5.456 -> "5.46%"
            "payout_usd": f"${payout_usd:.4f}",  # e.g. "$12.3456"
            "payout_sol": f"{payout_sol:.6f} SOL"  # e.g. "0.0032 SOL"
        }

        # return {
        #     "name": name,
        #     "address": address,
        #     "price": price,
        #     "market_cap": get_estimated_market_cap(address),  # Not provided in PumpSwap response
        #     "profit": profit,
        #     "payout_usd": payout_usd,
        #     "payout_sol": payout_sol
        # }
    except Exception as e:
        print(f"Error extracting token info: {e}")
        return None


def calculate_profit_and_payout(trade, current_price, sol_price_usd):
    try:
        initial_price = trade.initial_price or 0
        if initial_price == 0:
            return 0.0, 0.0
        profit_percent = ((current_price - initial_price) / initial_price) * 100
        payout_value_usd = current_price * (trade.purchased_token_amount or 0)
        payout_value_sol = payout_value_usd / sol_price_usd
        return round(profit_percent, 2), round(payout_value_usd, 4), round(payout_value_sol, 6)
    except:
        return 0.0, 0.0, 0.0


def get_estimated_market_cap(token_address: str) -> float | None:
    """
    Estimate token market cap using: market_cap = circulating_supply * usd_price.
    Priority:
    1. Try Solana RPC to get token supply
    2. Fallback to Jupiter API
    """

    try:
        # Fetch token price from Moralis (or your existing method)
        price_info = get_token_symbol_and_price(token_address)
        usd_price = price_info.get("usdPrice")
        if not usd_price:
            print("[MarketCap] Price not available.")
            return None
        # 1️⃣ Try Solana RPC for supply
        try:
            pubkey = Pubkey.from_string(token_address)
            response = solana_client.get_token_supply(pubkey)
            if response.value:
                supply = int(response.value.amount)
                decimals = int(response.value.decimals)
                adjusted_supply = supply / (10 ** decimals)
                return adjusted_supply * usd_price
        except Exception as e:
            print(f"[MarketCap] Solana RPC fallback failed: {e}")
        # Try Jupiter token API fallback
        try:
            url = f"https://lite-api.jup.ag/tokens/v1/token/{token_address}"
            resp = requests.get(url)
            if resp.ok:
                data = resp.json()
                supply = float(data.get("supply", 0))
                decimals = int(data.get("decimals", 0))
                adjusted_supply = supply / (10 ** decimals)
                return adjusted_supply * usd_price
        except Exception as e:
            print(f"[MarketCap] Jupiter API fallback failed: {e}")

        return None

    except Exception as e:
        print(f"[MarketCap] Unexpected error: {e}")
        return None
