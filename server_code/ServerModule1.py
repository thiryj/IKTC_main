# anvil section
import anvil.server
import anvil.secrets
import anvil.tables.query as q
from anvil.tables import app_tables

# public lib sectoin
import math
from typing import Dict, Tuple
import datetime
import json
from tradier_python import TradierAPI

# personal lib section
import server_helpers
import server_config
import positions

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
def get_open_trades_for_dropdown():
  """
    Fetches all open trades and formats them as a list
    of (display_text, item) tuples for a DropDown.
    """
  open_trades = app_tables.trades.search(Status='Open')
  dropdown_items = []

  for trade in open_trades:
    short_strike = "N/A"
    long_strike = "N/A"

    try:
      # 1. Find all transactions for this trade
      trade_transactions = app_tables.transactions.search(Trade=trade)

      # 2. Find all active legs for these transactions
      active_legs = app_tables.legs.search(
        Transaction=q.any_of(*trade_transactions),
        active=True
      )

      # 3. Find the short and long strikes from the active legs
      # (This assumes a simple 2-leg spread for the display)
      for leg in active_legs:
        if leg['Action'] in server_config.OPEN_ACTIONS: # e.g., 'Sell to Open'
          short_strike = leg['Strike']
        elif leg['Action'] in server_config.OPEN_ACTIONS: # e.g., 'Buy to Open'
          # This logic assumes the first 'Open' is short, the next is long
          # A more robust way is to check the action text precisely
          if leg['Strike'] != short_strike:
            long_strike = leg['Strike']

            # This is a cleaner, more direct query if your actions are distinct
      short_leg = app_tables.legs.search(Transaction=q.any_of(*trade_transactions), active=True, Action='Sell to Open')
      long_leg = app_tables.legs.search(Transaction=q.any_of(*trade_transactions), active=True, Action='Buy to Open')

      if short_leg:
        short_strike = short_leg[0]['Strike']
      if long_leg:
        long_strike = long_leg[0]['Strike']

    except Exception as e:
      print(f"Error finding legs for dropdown: {e}")
      pass # Will just display N/A for strikes

      # 4. Format the new display text
    open_date_str = trade['OpenDate'].strftime('%Y-%m-%d')
    display_text = (
      f"{trade['Underlying']} ({short_strike} / {long_strike}) "
      f"Opened: {open_date_str}"
    )

    dropdown_items.append( (display_text, trade) )

  return dropdown_items
  
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
  open_trades_list = list(app_tables.trades.search(Status=q.full_text_match('Open')))
  #print(f"Found {len(open_trades_list)} open trades.")
  return open_trades_list

@anvil.server.callable
def get_open_trades_with_risk(environment: str='SANDBOX'):
  """
    Fetches all open trades, then enriches them with live
    pricing and assignment risk data from the Tradier API.
    """
  open_trades = app_tables.trades.search(Status='Open')
  print(f"Found {len(open_trades)} open trades to analyze...")
  enriched_trades_list = []

  tradier_client, endpoint_url = server_helpers.get_tradier_client(environment)

  for trade in open_trades:
    trade_dto = {
      'Underlying': trade['Underlying'],
      'Strategy': trade['Strategy'],
      'OpenDate': trade['OpenDate'],
      'extrinsic_value': None, # Placeholder
      'is_at_risk': False       # Placeholder
    }
    
    try:
      # 1. Find the active short leg (your existing query)
      trade_transactions = app_tables.transactions.search(Trade=trade)
      current_short_leg = app_tables.legs.search(
        Transaction=q.any_of(*trade_transactions), # Find legs for any of these transactions
        active=True,                               # That is flagged as 'active'
        Action=q.any_of(*server_config.OPEN_ACTIONS) # And is a short leg (optional, but good)
      )[0] # Get the first (and hopefully only) one

      # 2. Get live underlying price
      underlying_symbol = trade['Underlying']
      underlying_price = get_underlying_quote(environment, underlying_symbol)

      # 3. Build the OCC symbol for the short leg
      occ_symbol = server_helpers.build_occ_symbol(
        underlying=underlying_symbol,
        expiration_date=current_short_leg['Expiration'],
        option_type=current_short_leg['OptionType'],
        strike=current_short_leg['Strike']
      )
      # 4. Get live option price
      option_quote = server_helpers.get_quote(environment, occ_symbol)
      option_price = option_quote.bid # Use bid price for a short option

      # 5. Calculate extrinsic value
      strike_price = current_short_leg['Strike']

      if current_short_leg['OptionType'] == server_config.OPTION_TYPE_PUT:
        intrinsic_value = max(0, strike_price - underlying_price)
        extrinsic_value = option_price - intrinsic_value
      elif current_short_leg['OptionType'] == server_config.OPTION_TYPE_CALL: 
        intrinsic_value = max(0, underlying_price - strike_price)
        extrinsic_value = option_price - intrinsic_value
      else:
        print("bad option type in get open trades with risk")

      # 6. Populate the DTO with the new data
      trade_dto['extrinsic_value'] = extrinsic_value

      # Our risk rule: ITM and extrinsic is less than $0.10
      if intrinsic_value > 0 and extrinsic_value < server_config.ASSIGNMENT_RISK_THRESHOLD:
        trade_dto['is_at_risk'] = True
      
    except Exception as e:
      print(f"Could not analyze risk for {trade['Underlying']}: {e}")
      pass # This trade will be skipped
      
    enriched_trades_list.append(trade_dto)
    # get live quotes, and do the risk calculation.
    # 3. Return the new list of DTOs
  return enriched_trades_list
  
@anvil.server.callable
def get_closed_trades():
  """Fetches all trades with a status of 'Closed'."""
  return app_tables.trades.search(Status='Closed')

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
    if order_details:
      return order_details.status
    return "unknown"

  except Exception as e:
    print(f"Error getting order status: {e}")
    return "error"

@anvil.server.callable
def cancel_order(environment: str, order_id: int):
  """
  Cancels a specific pending order.
  """
  try:
    tradier_client = server_helpers.get_tradier_client(environment)
    cancel_response_object = tradier_client.cancel_order(order_id)
    print(f"Cancel response for order {order_id}: {cancel_response_object}")

    if cancel_response_object:
      # You'll need to adjust this to access the status
      # e.g., return cancel_response_object.status
      print(f"cancel response: {cancel_response_object}")
      return "Order canceled" 
    return "Unknown"

  except Exception as e:
    print(f"Error canceling order: {e}")
    return "Error"

@anvil.server.callable
def get_active_legs_for_trade(trade_row):
  """
    Finds all 'active' leg rows associated with a single trade.
    """
  try:
    # 1. Find all transactions for this trade
    trade_transactions = app_tables.transactions.search(Trade=trade_row)

    # 2. Find all legs for those transactions that are 'active'
    active_legs = app_tables.legs.search(
      Transaction=q.any_of(*trade_transactions),
      active=True
    )

    # 3. Return the list of leg rows
    return list(active_legs)

  except Exception as e:
    print(f"Error getting active legs: {e}")
    return []

@anvil.server.callable
def save_manual_trade(transaction_type, trade_date, net_price, legs_data, 
                      underlying=None, existing_trade_row=None):
  """
    Saves a manual transaction.
    - If 'underlying' is provided, it creates a NEW trade.
    - If 'existing_trade_row' is provided, it adds a transaction TO that trade.
    """

  new_trade = None # This will hold the trade we're working with

  try:
    # --- 2. THIS IS THE NEW LOGIC ---
    if existing_trade_row:
      # We are adding to an existing trade
      print(f"Adding transaction to existing trade: {existing_trade_row['Underlying']}")
      new_trade = existing_trade_row

      # If this is a "Close" transaction, update the parent trade's status
      if 'Close:' in transaction_type:
        existing_trade_row.update(Status='Closed', CloseDate=trade_date)

    elif underlying:
      # We are creating a NEW trade
      print(f"Creating new trade for: {underlying}")
      status = 'Open' if 'Open:' in transaction_type else 'Closed'

      new_trade = app_tables.trades.add_row(
        Underlying=underlying,
        Strategy=transaction_type,
        Status=status,
        OpenDate=trade_date
      )
    else:
      raise ValueError("Must provide either an existing trade or a new underlying.")
      # --- END OF NEW LOGIC ---


      # 3. Create the 'Transactions' row (this logic is now universal)
    new_transaction = app_tables.transactions.add_row(
      Trade=new_trade,  # Link to the trade (either new or found)
      TransactionDate=trade_date,
      TransactionType=transaction_type,
      CreditDebit=net_price 
    )

    # 4. Loop through the legs (this logic is also universal)
    for leg in legs_data:
      is_active = leg['action'] in server_config.OPEN_ACTIONS

      app_tables.legs.add_row(
        Transaction=new_transaction,
        Action=leg['action'],
        Quantity=leg['quantity'],
        OptionType=leg['type'],
        Expiration=leg['expiration'],
        Strike=leg['strike'],
        active=is_active
      )

    return "Trade saved successfully!"

  except Exception as e:
    print(f"Error saving manual trade: {e}")
    return f"Error: {e}"