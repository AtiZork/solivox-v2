from solana.rpc.api import Client

# for local
secure_directory = r"C:\Users\Mudassar\Documents\Musaddaq Data"  # Customize your path here
#for live server
# secure_directory = '/home/rwts/Documents'  # Customize your path here
# for live
# Connect to your local Solana node
solana_client = Client("https://api.mainnet-beta.solana.com")
# solana_client = Client("http://127.0.0.1:8899")
SOLANA_WS_URL = Client("wss://api.mainnet-beta.solana.com")
PUMP_FUN_PROGRAM_ID_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

moraliz_api_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6ImNmMTVhODM4LTk3NmUtNDUxNS05Njc3LTE0YTUyNWRjMTc4NCIsIm9yZ0lkIjoiNDQxMDc5IiwidXNlcklkIjoiNDUzNzk2IiwidHlwZUlkIjoiNjgwOThlNjctNjJkYS00YTNjLTk2MDctNjkzNzZiOGQyZWFkIiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NDQzNTIwMjUsImV4cCI6NDkwMDExMjAyNX0.OkLc-k1tRX-J0uZNJ30i3w8dcCGp5jHOI9VeOGnE5mc"
Solcan_api_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjcmVhdGVkQXQiOjE3NDk2Mjg2OTE3MTIsImVtYWlsIjoibXVzYWRkYXFhYmJhczk2QGdtYWlsLmNvbSIsImFjdGlvbiI6InRva2VuLWFwaSIsImFwaVZlcnNpb24iOiJ2MiIsImlhdCI6MTc0OTYyODY5MX0.kKzTK1hz-c5NBw80Mb4P7hiIHbH81fUQwSVBS10L0Wo"
