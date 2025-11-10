# anvil section
import anvil.server
import anvil.secrets
import anvil.tables.query as q
from anvil.tables import app_tables, Row

# public lib sectoin
import math
from typing import Dict, Tuple
import datetime
import json
from tradier_python import TradierAPI, Position

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
      'trade_row': trade,
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

# In ServerModule1.py

@anvil.server.callable
def save_manual_trade(transaction_type, trade_date, net_price, legs_data, 
                      underlying=None, existing_trade_row=None):

  print(f"Server saving: {transaction_type}")
  new_trade = None

  try:
    # --- 1. Find or Create the Trade (Your existing logic) ---
    if existing_trade_row:
      new_trade = existing_trade_row
      if 'Close:' in transaction_type:
        existing_trade_row.update(Status='Closed', CloseDate=trade_date)
    elif underlying:
      status = 'Open' if 'Open:' in transaction_type else 'Closed'
      new_trade = app_tables.trades.add_row(
        Underlying=underlying,
        Strategy=transaction_type,
        Status=status,
        OpenDate=trade_date
      )
    else:
      raise ValueError("Must provide either an existing trade or a new underlying.")

      # --- 2. Create the Transaction (Your existing logic) ---
    new_transaction = app_tables.transactions.add_row(
      Trade=new_trade,
      TransactionDate=trade_date,
      TransactionType=transaction_type,
      CreditDebit=net_price 
    )

    # --- 3. Loop through legs & UPDATE ACTIVE FLAGS ---
    for leg in legs_data:
      action_string = leg['action']
      is_active_flag = action_string in server_config.OPEN_ACTIONS

      # --- THIS IS THE NEW LOGIC ---
      if not is_active_flag:
        # This is a closing action (e.g., "Buy to Close").
        # We must find the corresponding 'active' leg and deactivate it.

        # Determine the opposite 'open' action
        open_action = None
        if action_string == 'Buy to Close':
          open_action = 'Sell to Open'
        elif action_string == 'Sell to Close':
          open_action = 'Buy to Open'

        if open_action:
          # Find all transactions for this trade
          trade_transactions = app_tables.transactions.search(Trade=new_trade)

          try:
            # Find the active leg that matches
            leg_to_close = app_tables.legs.search(
              Transaction=q.any_of(*trade_transactions),
              active=True,
              Action=open_action,
              Strike=leg['strike'],
              Expiration=leg['expiration']
            )[0] # Find the first match

            # Deactivate the original leg
            leg_to_close.update(active=False)
          except Exception as e:
            print(f"Warning: Could not find matching active leg to close: {e}")
            # This can happen if the data is out of sync, but we
            # still want to record the closing transaction.
            # --- END OF NEW LOGIC ---

            # 4. Add the new leg row (for this transaction)
      app_tables.legs.add_row(
        Transaction=new_transaction,
        Action=action_string,
        Quantity=leg['quantity'],
        OptionType=leg['type'],
        Expiration=leg['expiration'],
        Strike=leg['strike'],
        active=is_active_flag # This will be False for "Close" actions
      )
    print("starting pl update")  
    if 'Close:' in transaction_type:
      # Now that the closing transaction is saved, sum the P/L
      # Find all transactions for this trade
      all_transactions = app_tables.transactions.search(Trade=new_trade)
      print(f"all transactions: {all_transactions}")
      total_pl = 0
      
      for t in all_transactions:
        if t['CreditDebit'] is not None:
          total_pl += t['CreditDebit']
          print(f"total PL: {total_pl}")

          # Update the parent trade row with the final numbers
      new_trade.update(
        Status='Closed', 
        CloseDate=trade_date,
        TotalPL=total_pl
      )
    return "Trade saved successfully!"

  except Exception as e:
    print(f"Error saving manual trade: {e}")
    return f"Error: {e}"

@anvil.server.callable
def validate_manual_legs(environment, legs_data_list):
  """
    Checks if all legs in a list are valid tradable options.
    Returns True if all are valid, or an error string if one fails.
    """
  for leg in legs_data_list:
    # Build the OCC symbol just like your risk function does
    occ_symbol = server_helpers.build_occ_symbol(
      underlying=leg['underlying'], # You'll need to pass this in
      expiration_date=leg['expiration'],
      option_type=leg['type'],
      strike=leg['strike']
    )

    quote = server_helpers.get_quote(environment, occ_symbol)

    if quote is None:
      return f"Invalid leg: {occ_symbol}"

  return True

@anvil.server.callable
def get_roll_package_dto(environment: str, trade_row: Row):
  """
    Finds active legs, gets live prices, and calculates
    a full 4-leg roll package with standardized keys.
    """

  # --- 1. Get Live Quotes for CURRENT Active Legs ---
  short_leg_quote = None
  long_leg_quote = None
  short_leg_db = None
  long_leg_db = None

  try:
    trade_transactions = app_tables.transactions.search(Trade=trade_row)
    short_leg_db = app_tables.legs.search(
      Transaction=q.any_of(*trade_transactions),
      active=True, Action='Sell to Open'
    )[0]
    long_leg_db = app_tables.legs.search(
      Transaction=q.any_of(*trade_transactions),
      active=True, Action='Buy to Open'
    )[0]

    short_occ = server_helpers.build_occ_symbol(
      underlying=trade_row['Underlying'],
      expiration_date=short_leg_db['Expiration'],
      option_type=short_leg_db['OptionType'],
      strike=short_leg_db['Strike']
    )
    long_occ = server_helpers.build_occ_symbol(
      underlying=trade_row['Underlying'],
      expiration_date=long_leg_db['Expiration'],
      option_type=long_leg_db['OptionType'],
      strike=long_leg_db['Strike']
    )

    short_leg_quote = server_helpers.get_quote(environment, short_occ)
    long_leg_quote = server_helpers.get_quote(environment, long_occ)

    if not short_leg_quote or not long_leg_quote:
      raise Exception("Could not get live quotes for active legs.")

  except Exception as e:
    raise Exception(f"Error finding active legs: {e}")

    # --- 2. Calculate Closing Cost & Build Closing Leg Dicts ---
  current_spread = positions.DiagonalPutSpread(short_leg_quote, long_leg_quote)
  total_close_cost = current_spread.calculate_cost_to_close()

  # Build standardized dicts for the closing legs
  closing_leg_1 = {
    'action': 'Buy to Close',
    'type': short_leg_db['OptionType'],
    'strike': short_leg_db['Strike'],
    'expiration': short_leg_db['Expiration'],
    'quantity': short_leg_db['Quantity']
  }
  closing_leg_2 = {
    'action': 'Sell to Close',
    'type': long_leg_db['OptionType'],
    'strike': long_leg_db['Strike'],
    'expiration': long_leg_db['Expiration'],
    'quantity': long_leg_db['Quantity']
  }
  closing_legs_list = [closing_leg_1, closing_leg_2]

  # --- 3.  Find NEW Legs ---
  # call find_new_diagonal_trade with the closing legs as the third arg
  new_spread_object = server_helpers.find_new_diagonal_trade(environment, trade_row['Underlying'], current_spread)
  #print(f"new spread is: {new_spread}")

  # --- 4. Calculate Opening Credit & Build Opening Leg Dicts (FIXED) ---
  total_open_credit = new_spread_object.calculate_net_premium()
  #print(f"open credit of roll to: {total_open_credit}")

  # prepare for serialization
  new_spread_dto = new_spread_object.get_dto()
  new_short_leg_dto = new_spread_dto['short_put']
  new_long_leg_dto = new_spread_dto['long_put']
  
  # Build standardized dicts for the opening legs
  opening_leg_1 = {
    'action': 'Sell to Open',
    'type': new_short_leg_dto['option_type'],
    'strike': new_short_leg_dto['strike'],
    'expiration': new_short_leg_dto['expiration_date'],
    'quantity': 1 # Assuming quantity 1
  }
  opening_leg_2 = {
    'action': 'Buy to Open',
    'type': new_long_leg_dto['option_type'],
    'strike': new_long_leg_dto['strike'],
    'expiration': new_long_leg_dto['expiration_date'],
    'quantity': 1 # Assuming quantity 1
  }
  opening_legs_list = [opening_leg_1, opening_leg_2]
  #print(f" open leg list: {opening_legs_list}")

  # --- 5. Package and Return ---
  all_4_legs = closing_legs_list + opening_legs_list
  total_roll_credit = total_open_credit - total_close_cost

  #print(f"in get_roll: roll legs:{all_4_legs}, roll credit: {total_roll_credit}")

  return {
    'legs_to_populate': all_4_legs,
    'total_roll_credit': total_roll_credit
  }

@anvil.server.callable
def get_new_open_trade_dto(environment: str, symbol: str) -> Dict:
  """
    This is the new "wrapper" for the 'Find New Trade' button.
    It calls the main engine and returns a serializable DTO.
    """

  # 1. Call your main engine (which is now a helper)
  best_position_object = server_helpers.find_new_diagonal_trade(
    environment=environment,
    underlying_symbol=symbol,
    position_to_roll=None  # We pass None to trigger 'open' logic
  )

  # 2. Check the result
  if not best_position_object:
    return None # Or return an error string

    # 3. Convert the object to a DTO and return it
  return best_position_object.get_dto()