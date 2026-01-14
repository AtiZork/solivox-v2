import requests

x_api_key = "<YOUR-API-KEY>"  # Replace with your actual API key

token_addresses = [
    "<MINT-ADDRESS>"
]

def get_price(ca):
    url = f"https://api.coinvera.io/api/v1/price?ca={ca}"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": x_api_key
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # raises HTTPError for bad responses
        data = response.json()
        return {'ca': ca, **data}
    except requests.exceptions.RequestException as err:
        return {'ca': ca, 'error': str(err)}

def get_prices_for_all_tokens():
    results = [get_price(ca) for ca in token_addresses]
    for res in results:
        if 'error' in res:
            print(f"Error for {res['ca']}: {res['error']}")
        else:
            print(f"Token: {res['ca']}")
            print(res)
            print('-------------------------')

