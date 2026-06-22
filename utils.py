from moralis import sol_api
from settings import moraliz_api_key, solana_client
import requests
from solders.pubkey import Pubkey
from live_pricing import pump_price
from flask import  jsonify
import live_pricing

def pump_price(mint):
    """Compute token price in SOL using Pump.fun bonding-curve, falling back to PumpSwap pool.
    """
    mint = mint.strip()
    if not mint:
        return ({"status": "failed", "message": "Missing 'mint'"})

    # 1) Try Pump.fun bonding-curve reserves at 8/16 like the JS reference
    try:
        mint_pk = Pubkey.from_string(mint)
    except Exception as e:
        return ({"status": "failed", "message": f"Invalid mint: {e}"})
    # Fetch token metadata (name, symbol) via Metaplex on-chain
    token_name, token_symbol = live_pricing._get_token_metadata_name_symbol(mint)

    if token_name is None or token_symbol is None:
        return None

    bonding_pda = live_pricing._pumpfun_bonding_curve_pda(mint)
    raw = live_pricing._get_account_data_bytes(bonding_pda)
    if raw:
        vt = live_pricing._read_u64_le(raw, 8)
        vs = live_pricing._read_u64_le(raw, 16)
        if vt > 0:
            price = float(vs) / (float(vt) * 1000.0)
            sol_price_usd = live_pricing._get_sol_price_usd_from_pyth()
            resp = {
                "source": "pumpfun",
                "mint": mint,
                "name": token_name,
                "symbol": token_symbol,
                "bondingcurve": str(bonding_pda),
                "virtual_token_reserves": int(vt),
                "virtual_sol_reserves": int(vs),
                "price_in_sol": price,
                "rpc_url": live_pricing.RPC_URL,
            }
            if sol_price_usd is not None:
                resp["sol_price_usd"] = sol_price_usd
                resp["usdPrice"] = price * sol_price_usd
            return resp

    # 2) Fallback to Pump AMM (PumpSwap) vault price
    # pool-authority PDA: ["pool-authority", mint] under Pump.fun program
    pool_auth_pda, _ = Pubkey.find_program_address([b"pool-authority", bytes(mint_pk)], live_pricing.PUMP_FUN_PROGRAM_ID)
    # pool PDA: ["pool", indexLE16(0), pool_auth_pda, mint, wsol]
    index_le16 = (0).to_bytes(2, byteorder="little", signed=False)
    pool_pda, _ = Pubkey.find_program_address(
        [b"pool", index_le16, bytes(pool_auth_pda), bytes(mint_pk), bytes(live_pricing.WSOL_MINT)],
        live_pricing.PUMP_AMM_PROGRAM_ID
    )
    base_vault = live_pricing._get_associated_token_address(mint_pk, pool_pda)
    quote_vault = live_pricing._get_associated_token_address(live_pricing.WSOL_MINT, pool_pda)

    base_amt, base_dec = live_pricing._get_token_account_amount_and_decimals(str(base_vault))
    quote_amt, quote_dec = live_pricing._get_token_account_amount_and_decimals(str(quote_vault))
    if base_amt is None or quote_amt is None or base_dec is None or quote_dec is None or base_amt == 0:
        return ({
            "status": "failed",
            "message": "Unable to fetch pool vault balances",
            "pool": str(pool_pda),
            "base_vault": str(base_vault),
            "quote_vault": str(quote_vault),
        })

    base_reserve = base_amt / (10 ** base_dec) if base_dec else 0.0
    quote_reserve = quote_amt / (10 ** quote_dec) if quote_dec else 0.0
    if base_reserve <= 0:
        return {"status": "failed", "message": "Base reserve is zero"}
    price = quote_reserve / base_reserve
    sol_price_usd = live_pricing._get_sol_price_usd_from_pyth()
    resp = {
        "source": "pumpswap",
        "mint": mint,
        "name": token_name,
        "symbol": token_symbol,
        "pool": str(pool_pda),
        "base_vault": str(base_vault),
        "quote_vault": str(quote_vault),
        "base_reserve": base_reserve,
        "quote_reserve": quote_reserve,
        "price_in_sol": price,
        "rpc_url": live_pricing.RPC_URL,
    }
    if sol_price_usd is not None:
        resp["sol_price_usd"] = sol_price_usd
        resp["usdPrice"] = price * sol_price_usd
    return resp

def get_token_symbol_and_price(mint_address: str):
    try:
        API_KEY = "jup_a1d0b05ce8142c073eb08772a4e6b3cb26449466bc702d5a949b3ebd1f8a32f6"

        headers = {
            "x-api-key": API_KEY
        }

        SOL_MINT = "So11111111111111111111111111111111111111112"

        # Token Metadata
        metadata_url = f"https://api.jup.ag/tokens/v2/search?query={mint_address}"
        meta_response = requests.get(metadata_url, headers=headers)

        token_name = "Unknown"
        token_symbol = "Unknown"

        if meta_response.status_code == 200:
            meta_data = meta_response.json()

            if meta_data and len(meta_data) > 0:
                token_name = meta_data[0].get("name", "Unknown")
                token_symbol = meta_data[0].get("symbol", "Unknown")

        # Token Price + SOL Price
        price_url = f"https://api.jup.ag/price/v3?ids={mint_address},{SOL_MINT}"
        price_response = requests.get(price_url, headers=headers)

        if price_response.status_code == 200:
            price_data = price_response.json()

            if mint_address in price_data:

                token_data = price_data[mint_address]

                # Token USD Price
                token_usd_price = round(
                    float(token_data.get("usdPrice", 0)),
                    6
                )

                # SOL USD Price
                sol_usd_price = None

                if SOL_MINT in price_data:
                    sol_usd_price = round(
                        float(price_data[SOL_MINT].get("usdPrice", 0)),
                        6
                    )

                result = {
                    **token_data,
                    "usdPrice": token_usd_price,
                    "name": token_name,
                    "symbol": token_symbol,
                    "sol_price_usd": sol_usd_price
                }

                print(result)
                return result

        return None

    except Exception as e:
        print(f"Error: {e}")
        return None

def get_token_symbol_and_prices(mint_address: str):
    try:
        # Your Jupiter API Key
        API_KEY = "jup_a1d0b05ce8142c073eb08772a4e6b3cb26449466bc702d5a949b3ebd1f8a32f6"

        headers = {
            "x-api-key": API_KEY
        }
        # Step 1: Fetch Token Metadata (Name & Symbol)
        metadata_url = f"https://api.jup.ag/tokens/v2/search?query={mint_address}"
        meta_response = requests.get(metadata_url, headers=headers)

        token_name = "Unknown"
        token_symbol = "Unknown"

        if meta_response.status_code == 200:
            meta_data = meta_response.json()
            # Tokens V2 search return an array/list of matching tokens
            if meta_data and len(meta_data) > 0:
                token_name = meta_data[0].get("name", "Unknown")
                token_symbol = meta_data[0].get("symbol", "Unknown")

        # Step 2: Fetch Token Price
        price_url = f"https://api.jup.ag/price/v3?ids={mint_address}"
        price_response = requests.get(price_url, headers=headers)

        if price_response.status_code == 200:
            price_data_ = price_response.json()

            # Extract matching data mapping cleanly
            if price_data_ and mint_address in price_data_:
                data = price_data_[mint_address]

                # Format price to 4 digits after dot
                if "usdPrice" in data and data["usdPrice"] is not None:
                    data["usdPrice"] = round(float(data["usdPrice"]), 6)

                # Inject metadata into the final response
                data["name"] = token_name
                data["symbol"] = token_symbol

                print(data)
                return data
            else:
                print("Price data not found for this mint address.")
                return None
        # params = {
        #     "network": "mainnet",
        #     "address": token_mint
        # }
        # if token_mint.endswith("pump"):
        #     result = pump_price(token_mint)
        #     if result is None:
        #         result = sol_api.token.get_token_price(
        #             api_key=moraliz_api_key,
        #             params=params,
        #         )
        # else:
        #     result = sol_api.token.get_token_price(
        #         api_key=moraliz_api_key,
        #         params=params,
        #     )
        # return result
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
