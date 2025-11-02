import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server
import anvil.secrets
from tradier_python import TradierAPI
import server_helpers
import server_config
import positions
import math
from typing import Dict, Tuple
import datetime
import json

# This is a server module. It runs on the Anvil server,
# rather than in the user's browser.
#
# To allow anvil.server.call() to call functions here, we mark
#
@anvil.server.callable
def get_tradier_profile(environment: str):
  try:
    tradier_client, endpoint_url = server_helpers.get_tradier_client(environment)
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
def get_underlying_quote(environment: str, symbol: str) ->float:
  # get underlying price and thus short strike
  t, endpoint_url = server_helpers.get_tradier_client(environment)
  underlying_quote = t.get_quotes([symbol, "bogus"], greeks=False)
  # note:  needed to send a fake symbol in because of a bug in the get_quotes endpoint
  underlying_price = underlying_quote[0].last
  print(f"Underlying price: {underlying_price}")
  return underlying_price
  
@anvil.server.callable
def get_tradier_positions(environment: str):
  """
  Gets an authenticated client, fetches positions, and returns the data.
  This function CAN be called by the client.
  """
  try:
    # Step 1: Get the authenticated client object
    tradier_client, endpoint_url = server_helpers.get_tradier_client(environment)

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
def find_new_diagonal_trade(environment='SANDBOX', 
                            underlying_symbol=None)->Dict:
  """
  This function will contain the logic to connect to Tradier,
  find a suitable short put diagonal, and return its parameters.
  """
  print("Server function 'find_new_diagonal_trade' was called.")
  if underlying_symbol is None:
    anvil.alert("must select underlying symbol")
    return
  t, endpoint_url = server_helpers.get_tradier_client(environment)
  underlying_price = get_underlying_quote(environment, underlying_symbol)
  short_strike = math.ceil(underlying_price)
  
  # get list of valid positions
  print("calling get_valid_diagonal_put_spreads")
  valid_positions = server_helpers.get_valid_diagonal_put_spreads(short_strike=short_strike, 
                                                                  tradier_client=t, 
                                                                  symbol=underlying_symbol, 
                                                                  max_days_out=server_config.MAX_DTE)
  number_positions = len(valid_positions)
  print(f"Number of valid positions: {len(valid_positions)}")
  if number_positions == 0:
    print("Halting script - no positions")
    return "no positions"
  
  # find best put diag based on highest return on margin per day of trade
  best_position = max(
    valid_positions,
    key=lambda pos: pos.ROM_rate,
    default=None
  )
  if not best_position:
    print("No good roll to position identified")
    return 1
  
  print('To Open')
  best_position.print_leg_details()
  best_position.describe()
  best_position_dto = best_position.get_dto()
  return best_position_dto

@anvil.server.callable
def submit_order(environment: str='SANDBOX', 
                           underlying_symbol: str=None,
                           trade_dto: Dict=None, 
                           quantity: int=1,
                           preview: bool=True,
                           limit_price: float=None,
                           trade_type: str=None)->Dict:
  # verify symbol and positions are present
  if underlying_symbol is None or trade_dto is None:
    print("no symbol or position in submit_preview_order")
  # get client and endpoint
  t, endpoint_url = server_helpers.get_tradier_client(environment)

  # submit order
  trade_response = server_helpers.submit_diagonal_spread_order(t, 
                                                              endpoint_url, 
                                                              underlying_symbol, 
                                                              quantity, 
                                                              trade_dto, 
                                                              preview,
                                                              limit_price,
                                                            trade_type)
  print(f"trade response: {trade_response}")
  return trade_response
  
def submit_real_order(environment='SANDBOX', position: positions.DiagonalPutSpread=None):
  pass
"""
  # since preview was good, submit real order
  if server_config.PLACE_TRADE:
    # skip on Friday if enabled
    if not server_config.ROLL and datetime.date.today().weekday() in server_config.DAYS_TO_NOT_OPEN:
      print("Don't open new positions on Friday.  Override by editing flag")
      return
      # ask user if they want to over ride price
    limit_price_str = input("Input limit price or 'M' to use calculated market price:")
    try:
      limit_price = float(limit_price_str)
    except ValueError:
      limit_price = None
      # handle user confirmation if enabled
    if server_config.USER_CONFIRMATION:
      trade_auth_response = input("Send Trade? (Y or N):")
      if trade_auth_response != "Y":
        print("Trade cancelled by user")
        return
      else:
        print("Trade authorized by user")
    order_data = server_helpers.submit_diagonal_spread_order(t, 
                                                      endpoint_url, 
                                                      underlying_symbol,
                                                      quantity, 
                                                      positions_list, 
                                                      preview=False,
                                                      limit_price=limit_price )

    if order_data and order_data.get('order', {}).get('status') == 'ok':
      print("--- Order Submitted Successfully ---")
      order_id = order_data['order']['id']
      print(f"Order ID: {order_id}")
    else:
      print("--- Failed to Submit Real Order ---")
      print(json.dumps(order_data, indent=2))
  else:
    print("Trading not enabled")
else:
  print("--- Order Preview Failed or Invalid ---")
  print(json.dumps(preview_data, indent=2))
"""

@anvil.server.callable
def get_quantity(best_position: positions.DiagonalPutSpread)->int:
  # calculate quantity based on fixed allocation.  
  #TODO: generalize this to lookup available capital t.get_account_balance().cash.cash_available
  quantity = math.floor(server_config.ALLOCATION / best_position.margin) if best_position.margin > 0 else 0
  quantity = 1 if server_config.TRADE_ONE else quantity
  return quantity

  # In your ServerModule1.py

@anvil.server.callable
def get_order_status(environment: str, order_id: int):
  """
    Checks the status of a specific order ID.
    """
  try:
    # Get your authenticated Tradier client
    tradier_client, endpoint = server_helpers.get_tradier_client(environment)

    # Make the API call to check the order
    # NOTE: The method name 'get_order' is an example. 
    # Use the actual method from your TradierAPI class.
    order_details = tradier_client.get_order(order_id)

    print(f"Status for order {order_id}: {order_details}")

    # Return the status part of the response
    if order_details and 'order' in order_details:
      return order_details['order']['status']
    return "unknown"

  except Exception as e:
    print(f"Error getting order status: {e}")
    return "error"