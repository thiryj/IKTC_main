import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server
import anvil.secrets
from tradier_python import TradierAPI
import server_helpers

# This is a server module. It runs on the Anvil server,
# rather than in the user's browser.
#
# To allow anvil.server.call() to call functions here, we mark
#
def get_tradier_client(environment: str)->TradierAPI:
  """
    Gets an authenticated Tradier client.
    Checks a module-level cache first. If not found, it creates, caches, and returns it.
    """
    
  env_prefix = environment.upper() # e.g., 'PROD' or 'SANDBOX'
  # Use square bracket dictionary-style access, not .get()
  api_key = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_API_KEY')
  account_id = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_ACCOUNT')
  endpoint_url = anvil.secrets.get_secret(f'{env_prefix}_ENDPOINT_URL')

  # Create the authenticated client object
  t = TradierAPI(
    token=api_key, 
    default_account_id=account_id, 
    endpoint=endpoint_url)
  return t

@anvil.server.callable
def get_tradier_profile(environment: str):
  try:
    tradier_client = get_tradier_client(environment)
    profile = tradier_client.get_profile()
    print(f"profile is: {profile}")
    if profile and profile.account:
      account_number = profile.account[0].account_number
      return {'account_number': account_number}
    else:
      return None
  except Exception as e:
    print(f"Error retrieving Tradier profile: {e}")
    raise e

@anvil.server.callable
def get_account_nickname(account_number_to_check):
  # Assumes you have secrets named 'PROD_ACCOUNT' and 'IRA_ACCOUNT'
  nicknames = {
    anvil.secrets.get_secret('PROD_TRADIER_ACCOUNT'): 'NQ',
    anvil.secrets.get_secret('IRA_TRADIER_ACCOUNT'): 'IRA',
    anvil.secrets.get_secret('SANDBOX_TRADIER_ACCOUNT'): 'Paper Trading'
  }
  return nicknames.get(account_number_to_check, "account nickname not found")
  
@anvil.server.callable
def get_tradier_positions(environment: str):
  """
  Gets an authenticated client, fetches positions, and returns the data.
  This function CAN be called by the client.
  """
  try:
    # Step 1: Get the authenticated client object
    tradier_client = get_tradier_client(environment)

    # Step 2: Use the client to make an API call
    positions_data = tradier_client.get_positions() # Assuming a method like this exists

    # Step 3: Return only the JSON-serializable data to the client
    print(f"Retrived {len(positions_data)} positions")
    return positions_data

  except Exception as e:
    # It's good practice to handle potential errors
    print(f"An error occurred: {e}")
    return e

@anvil.server.callable
def get_open_trades():
  """Fetches all trades with a status of 'Open'."""
  open_trades_list = list(tables.app_tables.trades.search(Status=q.full_text_match('Open')))
  #print(f"Found {len(open_trades_list)} open trades.")
  return open_trades_list
  
@anvil.server.callable
def get_closed_trades():
  """Fetches all trades with a status of 'Closed'."""
  return tables.app_tables.trades.search(Status='Closed')

@anvil.server.callable
def find_new_diagonal_trade():
  """
  This function will contain the logic to connect to Tradier,
  find a suitable short put diagonal, and return its parameters.
  """
  print("Server function 'find_new_diagonal_trade' was called.")

  # --- PASTE YOUR TRADIER SCRIPT LOGIC HERE ---
  # For now, we will just return a hardcoded example trade.

  example_trade = {
    'underlying': 'SPY',
    'strategy': 'Short Put Diagonal',
    'short_leg': {'strike': 450, 'expiry': '2025-11-21', 'price': 5.50},
    'long_leg': {'strike': 440, 'expiry': '2025-12-19', 'price': 4.00}
  }

  return example_trade