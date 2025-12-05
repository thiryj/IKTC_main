# anvil section
import anvil.server
import anvil.secrets
import anvil.tables.query as q
from anvil.tables import app_tables, Row

# public lib sectoin
import math
from typing import Dict, Tuple, List
import datetime as dt
import json
from tradier_python import TradierAPI, Position

# personal lib section
import server_helpers
import server_config
import positions
import config

# To allow anvil.server.call() to call functions here, we mark
#
@anvil.server.callable
def get_settings():
  # Attempt to get the first row
  settings_row = app_tables.settings.get()

  # If table is empty, initialize it with your preferred defaults
  if not settings_row:
    settings_row = app_tables.settings.add_row(
      default_symbol=config.DEFAULT_SYMBOL,
      defualt_qty=1, 
      use_max_qty=False, 
      refresh_timer_on=True
    )
  return settings_row
  
@anvil.server.callable
def get_tradier_profile(environment: str):
  try:
    tradier_client, endpoint_url = server_helpers.get_tradier_client(environment)
    profile = tradier_client.get_profile()
    print(f"profile account number: {profile.account[0].account_number}")
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
def get_underlying_price(environment: str, symbol: str) ->float:
  # get underlying price and thus short strike
  t, endpoint_url = server_helpers.get_tradier_client(environment)
  underlying_quote = server_helpers.get_quote(t, symbol)
  
  # Extract price: Use 'last' or fallback to 'close' 
  underlying_price = underlying_quote.get('last')
  if underlying_price is None:
    underlying_price = underlying_quote.get('close') 

  if underlying_price is None:
    raise ValueError(f"Price not available in API response for {symbol}")
  #print(f"underlying_price: {underlying_price}")
  return underlying_price

@anvil.server.callable
def get_open_trades_for_dropdown(environment: str=server_config.ENV_SANDBOX):
  """
    Fetches all open trades and formats them as a list
    of (display_text, item) tuples for a DropDown.
    """
  open_trades = app_tables.trades.search(
                                          Status='Open',
                                          Account=environment
  )
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
        if leg['Action'] in config.OPEN_ACTIONS: # e.g., 'Sell to Open'
          short_strike = leg['Strike']
        elif leg['Action'] in config.OPEN_ACTIONS: # e.g., 'Buy to Open'
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
def get_open_trades(environment: str=server_config.ENV_SANDBOX):
  """Fetches all trades with a status of 'Open'."""
  open_trades_list = list(app_tables.trades.search(Status=q.full_text_match('Open'),Account=environment))
  #print(f"Found {len(open_trades_list)} open trades.")
  return open_trades_list

@anvil.server.callable
def get_open_trades_with_risk(environment: str=server_config.ENV_SANDBOX, 
                              refresh_risk: bool=True)->Dict:
  """
    Fetches all open trades, then enriches them with live
    pricing and assignment risk data from the Tradier API and RROC.
    """

  #NOTE:  this only works for 2 leg spreads - need different logic for CSP or covered calls
  open_trades = app_tables.trades.search(Status='Open', Account=environment)
  #print(f"Found {len(open_trades)} open trades for {environment}")
  tradier_client, endpoint_url = server_helpers.get_tradier_client(environment)

  settings = app_tables.settings.get()
  target_daily_rroc = settings['default_target_rroc'] if settings and settings['default_target_rroc'] else 0.001
  enriched_trades_list = []
  
  for trade in open_trades:
    trade_dto = {
      'trade_row': trade,
      'Underlying': trade['Underlying'],
      'Strategy': trade['Strategy'],
      'Quantity': None,
      'OpenDate': trade['OpenDate'],
      'extrinsic_value': None, # Placeholder
      'is_at_risk': False,       # Placeholder
      'short_strike': None,
      'long_strike': None,
      'short_expiry': None,
      'rroc': "N/A",
      'harvest_price': "N/A",
      'is_harvestable': False
    }
    
    try:
      # 1. Find the active short leg (your existing query)
      trade_transactions = list(app_tables.transactions.search(Trade=trade))
      active_legs = list(app_tables.legs.search(
        Transaction=q.any_of(*trade_transactions), # Find legs for any of these transactions
        active=True                               # That is flagged as 'active'
      ))

      current_short_leg = None
      current_long_leg = None
      
      #print(f"Active legs found: {active_legs}")
      if trade['Strategy'] in (config.POSITION_TYPE_DIAGONAL, config.POSITION_TYPE_COVERED_CALL):
        current_short_leg = next((leg for leg in active_legs if leg['Action'] == server_config.SHORT_OPEN_ACTION), None)
        
        if trade['Strategy'] == config.POSITION_TYPE_DIAGONAL:
          #print(f"trade strategy: {trade['Strategy']} is identified: as position type:{config.POSITION_TYPE_DIAGONAL}")
          current_long_leg =  next((leg for leg in active_legs if leg['Action'] == server_config.LONG_OPEN_ACTION), None)
          if current_long_leg:
            trade_dto['long_strike'] = current_long_leg['Strike']
          else:
            print("missing long leg of the spread")
            continue
          
        else:
          #print(f"trade strategy: {trade['Strategy']} is identified: as position type:{config.POSITION_TYPE_COVERED_CALL}")
          pass
        if not current_short_leg:
          print("missing short leg for the spread or CSP")
          continue
        #print(f"current_short_leg is: {current_short_leg}")
        quantity = current_short_leg['Quantity']
        trade_dto['Quantity'] = quantity
        short_strike_price = current_short_leg['Strike']
        trade_dto['short_strike'] = current_short_leg['Strike']
        trade_dto['short_expiry'] = current_short_leg['Expiration']
      else:
        print(f"trade strategy: {trade['Strategy']} is not identified: as position type: {config.POSITION_TYPE_DIAGONAL} or position type: {config.POSITION_TYPE_COVERED_CALL} ")
        
      if refresh_risk and current_short_leg:
        try:
          # A. Get Margin from latest transaction
          # Sort transactions by date to get the most recent margin entry
          sorted_trans = sorted(trade_transactions, key=lambda x: x['TransactionDate'])
          latest_margin = sorted_trans[-1]['ResultingMargin'] if sorted_trans else 0

          # B. Get Days in Trade
          days_in_trade = (dt.date.today() - trade['OpenDate']).days
          days_in_trade = 1 if days_in_trade < 1 else days_in_trade
          
          # Get live underlying price
          underlying_symbol = trade['Underlying']
          underlying_price = get_underlying_price(environment, underlying_symbol)
          """
          # Short leg quote
          if trade['Underlying'] in config.INDEX_SYMBOLS:
            # slower API call 
            short_occ = server_helpers.lookup_option_symbol(
              tradier_client,
              trade['Underlying'],
              current_short_leg['Expiration'],
              current_short_leg['OptionType'],
              current_short_leg['Strike']
            )
          else:
            # offline faster version
            short_occ = server_helpers.build_occ_symbol(
              underlying=underlying_symbol,
              expiration_date=current_short_leg['Expiration'],
              option_type=current_short_leg['OptionType'],
              strike=current_short_leg['Strike']
            )
          # print(f"calling get_quote on: {occ_symbol}")
          short_quote = server_helpers.get_quote_direct(tradier_client, short_occ)
          print(f"in refresh: short_quote is: {short_quote}")

          # Long Leg Quote (if exists)
          long_quote = None
          if current_long_leg:
            long_occ = server_helpers.build_occ_symbol(
              underlying=trade['Underlying'],
              expiration_date=current_long_leg['Expiration'],
              option_type=current_long_leg['OptionType'],
              strike=current_long_leg['Strike']
            )
            long_quote = server_helpers.get_quote_direct(tradier_client, long_occ)
          """
          # 3. Fetch Quotes for Both Legs
          short_quote = server_helpers.fetch_leg_quote(tradier_client, trade['Underlying'], current_short_leg)
          long_quote = server_helpers.fetch_leg_quote(tradier_client, trade['Underlying'], current_long_leg)  

          # D. Calculate Current P/L
          # Sum collected credit (per share)
          total_credit_per_share = sum(t['CreditDebit'] for t in trade_transactions if t['CreditDebit'] is not None)

          # Calculate Cost to Close (per share)
          # Short: Buy to close (Ask) | Long: Sell to close (Bid)
          short_ask = short_quote.get('ask', 0) if short_quote else 0
          long_bid = long_quote.get('bid', 0) if long_quote else 0

          # Cost to close is Debit (buying back short) - Credits (selling long)
          cost_to_close_per_share = short_ask - long_bid

          # Net P/L Dollar Amount
          # (Total Credit - Cost to Close) * Quantity * 100

          current_pl_dollars = (total_credit_per_share - cost_to_close_per_share) * quantity * config.DEFAULT_MULTIPLIER

          # E. Final RROC Calculation
          # Avoid division by zero
          if latest_margin and latest_margin > 0:
            daily_rroc = (current_pl_dollars / latest_margin) / days_in_trade
            trade_dto['rroc'] = f"{daily_rroc:.2%}" # Format as percentage
            # flag if ready to harvest
            trade_dto['is_harvestable'] = True if daily_rroc >= config.DEFAULT_RROC_HARVEST_TARGET else False

            # Target Profit = Target Rate * Margin * Days
            target_profit_total = target_daily_rroc * latest_margin * days_in_trade
            target_profit_per_share = target_profit_total / (quantity * config.DEFAULT_MULTIPLIER)

            # Harvest Price = Credit Received - Target Profit
            harvest_price = total_credit_per_share - target_profit_per_share
            trade_dto['harvest_price'] = f"{harvest_price:.2f}"
          else:
            trade_dto['rroc'] = "0.00%"

          # do the assignment risk part
          option_price = short_quote.get('bid') # Use bid for risk calc
          intrinsic_value = 0
          if current_short_leg['OptionType'] == server_config.OPTION_TYPE_PUT:
            intrinsic_value = max(0, short_strike_price - underlying_price)
            #print(f"in put: intrinsic: {intrinsic_value}, extrinsic: {extrinsic_value}")
          elif current_short_leg['OptionType'] == server_config.OPTION_TYPE_CALL: 
            intrinsic_value = max(0, underlying_price - short_strike_price)
            #print(f"in call: intrinsic: {intrinsic_value}, extrinsic: {extrinsic_value}")
          else:
            print("bad option type in get open trades with risk")
          extrinsic_value = option_price - intrinsic_value
          trade_dto['extrinsic_value'] = extrinsic_value
    
          # Our risk rule: ITM and extrinsic is less than $0.10
          if intrinsic_value > 0 and extrinsic_value < server_config.ASSIGNMENT_RISK_THRESHOLD:
            trade_dto['is_at_risk'] = True
          
        except Exception as e:
          print(f"Error calculating RROC/Risk for {trade['Underlying']}: {repr(e)}")
          pass # do not return risk field

    except Exception as e:
      print(f"Could not load legs for {trade['Underlying']}: {repr(e)}")
      pass # This trade will be skipped
    #print(f"dto is: {trade_dto}")  
    enriched_trades_list.append(trade_dto)
    # get live quotes, and do the risk calculation.
    # 3. Return the new list of DTOs
  return enriched_trades_list

@anvil.server.callable
def get_closed_trades(environment: str=server_config.ENV_SANDBOX):
  closed_trades = app_tables.trades.search(Status='Closed', Account=environment)
  enriched_trades = []
  
  # Trade level accumulators
  total_pl_sum = 0.0
  trade_rroc_sum = 0.0
  trade_count = 0
  
  # Portfolio level accumulators
  total_margin_days = 0.0
  earliest_open_date = min((t['OpenDate'] for t in closed_trades), default=dt.date.today())
  has_trades = False
  
  for trade in closed_trades:
    has_trades = True
    trade_dict = dict(trade)

    #--Date data--
    open_date = trade['OpenDate'] or dt.date.today()
    close_date = trade['CloseDate'] or dt.date.today()
    dit = max(1, (close_date - open_date).days)
    
    # 1. Trade level max margin
    trans = app_tables.transactions.search(Trade=trade)
    max_margin = max([t['ResultingMargin'] for t in trans if t['ResultingMargin'] is not None], default=0)

    # get qty
    qty = 0
    if trans:
      trade_legs = app_tables.legs.search(Transaction=q.any_of(*trans))
      for leg in trade_legs:
        # We assume the quantity of the 'Open' leg represents the trade size
        if leg['Action'] in server_config.OPEN_ACTIONS:
          qty = leg['Quantity']
          break
    trade_dict['Quantity'] = qty
    
    # 2. Calc P/L & RROC
    pl = trade['TotalPL'] or 0.0
    total_pl_sum += pl

    # Trade level RROC
    if max_margin > 0:
      trade_daily_rroc = (pl / max_margin) / dit
      trade_dict['rroc'] = f"{trade_daily_rroc:.2%}"
      trade_rroc_sum += trade_daily_rroc
      trade_count += 1

      # Portfolio level margin-days
      total_margin_days += (max_margin * dit)
    else:
      trade_dict['rroc'] = "0.00%"
  
    enriched_trades.append(trade_dict)

  # Trade level rroc average
  avg_trade_rroc = (trade_rroc_sum / trade_count) if trade_count else 0.0

  # Portfolio level rroc performance
  portfolio_daily_rroc = 0.0
  if total_margin_days > 0:
    portfolio_daily_rroc = total_pl_sum / total_margin_days

  # 3. Sort & Return
  enriched_trades.sort(key=lambda x: x['OpenDate'] or dt.date.min, reverse=True)
  
  return {
    'trades': enriched_trades,
    'total_pl': total_pl_sum,
    'trade_rroc_avg': avg_trade_rroc,
    'portfolio_rroc_cum': portfolio_daily_rroc
  }

@anvil.server.callable
def submit_order(environment: str='SANDBOX', 
                           underlying_symbol: str=None,
                           trade_dto_list: List=[], # list of dicts with {spread meta..., 'short_put', 'long_put'}
                           quantity: int=1,
                           preview: bool=True,
                           limit_price: float=None
                           )->Dict:
    
  # verify symbol and positions are present
  if underlying_symbol is None or trade_dto_list is None:
    print("no symbol or position in submit_preview_order")
  # get client and endpoint
  t, endpoint_url = server_helpers.get_tradier_client(environment)

  #print(f"submit order: trade_dto_list: {trade_dto_list}")
  # submit order
  trade_response = server_helpers.submit_diagonal_spread_order(
                                                              t, 
                                                              endpoint_url, 
                                                              underlying_symbol, 
                                                              quantity, 
                                                              trade_dto_list, # list of dicts with {spread meta..., 'short_put', 'long_put'}
                                                              preview,
                                                              limit_price
                                                            )
  #print(f"trade response: {trade_response}")
  return trade_response
  
@anvil.server.callable
def get_quantity(best_position: positions.DiagonalPutSpread)->int:
  # calculate quantity based on fixed allocation.  
  #TODO: generalize this to lookup available capital t.get_account_balance().cash.cash_available
  quantity = math.floor(server_config.ALLOCATION / best_position.margin) if best_position.margin > 0 else 0
  quantity = 1 if config.TRADE_ONE else quantity
  return quantity

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
def save_manual_trade(environment: str, 
                      strategy: str, #Strategy: Diagonal, Covered Call
                      manual_entry_state: str, # OPEN or CLOSE or ROLL 
                      trade_date, 
                      net_price: float, 
                      legs_data_list,            # list of leg data entries.  may be 1 (CSP) or 2 (diag open/close) or 4 (roll)                    
                      existing_trade_row=None):  #CLOSE or ROLL: exising Trade row.  OPEN: None

  print(f"Server saving to environment {environment}: {strategy}")
  # --- 0. NEW: Safety Validation for Diagonals ---
  if strategy == config.POSITION_TYPE_DIAGONAL:
    try:
      # Filter for opening legs
      short_legs = [l for l in legs_data_list if l['action'] == config.ACTION_SELL_TO_OPEN]
      long_legs = [l for l in legs_data_list if l['action'] == config.ACTION_BUY_TO_OPEN]

      # If we have both (opening a new spread or rolling), check quantities
      if short_legs and long_legs:
        short_qty = short_legs[0]['quantity']
        long_qty = long_legs[0]['quantity']

        if short_qty != long_qty:
          print(f"WARNING: Mismatched quantities detected (Short: {short_qty}, Long: {long_qty}). Normalizing to Short qty.")
          # Force the long leg to match the short leg
          long_legs[0]['quantity'] = short_qty
          # Update the main list reference if needed (dictionaries are mutable, so this should stick)

    except Exception as e:
      print(f"Validation warning: {e}")
  underlying_symbol = legs_data_list[0]['underlying_symbol']  # can use any leg as they should all be the same underlying
  trade_row = None
  resulting_margin = 0.0
  
  try:
    # --- 1. Find or Create the Trade (Your existing logic) ---
    # update trade row Status for CLOSE/ROLL  
    if manual_entry_state in (config.MANUAL_ENTRY_STATE_CLOSE, config.MANUAL_ENTRY_STATE_ROLL):
      trade_row = existing_trade_row
      if manual_entry_state == config.MANUAL_ENTRY_STATE_CLOSE:
        existing_trade_row.update(Status=server_config.TRADE_ROW_STATUS_CLOSED, CloseDate=trade_date)      
    # Create new trade row for OPEN
    elif manual_entry_state == config.MANUAL_ENTRY_STATE_OPEN:
      trade_row = app_tables.trades.add_row(
        Underlying=underlying_symbol,
        Strategy=strategy,    # Strategy
        Status=server_config.TRADE_ROW_STATUS_OPEN,
        OpenDate=trade_date,
        Account=environment
      )
    else:
      raise ValueError("Manual Transaction Card State unknown")

    # --- 2. Calculate Margin ---
    # Logic: If we are OPENING or ROLLING, we need to find the NEW short and long legs 
    # to calculate the new margin requirement.
    if manual_entry_state != config.MANUAL_ENTRY_STATE_CLOSE:
      try:
        sell_to_open_dto_list = [leg for leg in legs_data_list if leg['action'] == config.ACTION_SELL_TO_OPEN]
        buy_to_open_dto_list = [leg for leg in legs_data_list if leg['action'] == config.ACTION_BUY_TO_OPEN]
        if sell_to_open_dto_list and buy_to_open_dto_list:
          short_strike = sell_to_open_dto_list[0]['strike']
          long_strike = buy_to_open_dto_list[0]['strike']
          quantity = sell_to_open_dto_list[0]['quantity']
          resulting_margin = abs(short_strike - long_strike) * quantity * config.DEFAULT_MULTIPLIER
      except Exception as e:
        resulting_margin = 0
        print(f"failed to get margin for open action: {e}")
    else:
      resulting_margin = 0
      
      # --- Create the Transaction  ---
    new_transaction = app_tables.transactions.add_row(
      Trade=trade_row,
      TransactionDate=trade_date,
      TransactionType=strategy,  # Strategy: Diagonal, Coverd Call, CSP, Stock, Misc
      CreditDebit=net_price,
      ResultingMargin=resulting_margin
    )

    # --- 3. Loop through legs & UPDATE ACTIVE FLAGS ---
    for leg in legs_data_list:
      action_string = leg['action']   # sell to open, etc
      is_open_action_flag = action_string in config.OPEN_ACTIONS   # setf flag to Ture if an open leg action, False if a closing leg action

      # Handle close action legs, open legs fall through to the leg row adder below
      if not is_open_action_flag:
        # This is a closing action (e.g., "Buy to Close").
        # We must find the corresponding 'active' leg and deactivate it.
        # Starting with the closing legs, Determine the opposite 'open' action 
        # wait:  aren't the original legs that are currently open passed in with the legs_data_list?  No, they are not, so we must deduce them from the new closing legs
        old_leg_open_action = None
        if action_string == config.ACTION_BUY_TO_CLOSE:
         old_leg_open_action  = config.ACTION_SELL_TO_OPEN
        elif action_string == config.ACTION_SELL_TO_CLOSE:
         old_leg_open_action = config.ACTION_BUY_TO_OPEN 

        if old_leg_open_action:
          # Find all original existing transactions for this trade that need to be marked as active=False
          trade_transactions = app_tables.transactions.search(Trade=trade_row)

          try:
            # Find the active leg that matches
            leg_to_deactivate = app_tables.legs.search(
              Transaction=q.any_of(*trade_transactions),
              active=True,
              Action=old_leg_open_action,
              Strike=leg['strike'],
              Expiration=leg['expiration']
            )[0] # Find the first match
            # Finally - Deactivate the original leg and repeat through rest of legs_data_list
            leg_to_deactivate.update(active=False)
          except Exception as e:
            print(f"Warning: Could not find matching active leg to close: {e}")

      # 4. Add the new leg row (for this transaction)    
      # runs for all legs in legs_data_list
      app_tables.legs.add_row(
        Transaction=new_transaction,
        Action=action_string,
        Quantity=leg['quantity'],
        OptionType=leg['option_type'],
        Expiration=leg['expiration'],
        Strike=leg['strike'],
        active=is_open_action_flag # This will be False for "Close" actions
      )
    print("starting pl update")  
    #if strategy and 'Close:' in strategy:
    #*************************UPDATE this*****************************************************************
    if manual_entry_state == config.MANUAL_ENTRY_STATE_CLOSE:
      # Now that the closing transaction is saved, sum the P/L
      # Find all transactions for this trade
      all_transactions = app_tables.transactions.search(Trade=trade_row)
      #print(f"all transactions: {all_transactions}")
      total_pl_dollars = 0
      
      for t in all_transactions:
        price = t['CreditDebit']
        if price is not None:
          # find legs for this transaction
          trans_legs = app_tables.legs.search(Transaction=t)
          quantity =1 # default
          if len(trans_legs) > 0:
            quantity = trans_legs[0]['Quantity']

          transaction_cash_value = price * quantity * config.DEFAULT_MULTIPLIER
          
          total_pl_dollars += transaction_cash_value
          #print(f"total PL: {transaction_cash_value}")

      # Update the parent trade row with the final numbers
      trade_row.update(
        Status=server_config.TRADE_ROW_STATUS_CLOSED, 
        CloseDate=trade_date,
        TotalPL=round(total_pl_dollars)
      )

    return "Trade saved successfully!"

  except Exception as e:
    print(f"Error saving manual trade: {e}")
    return f"Error: {e}"

@anvil.server.callable
def validate_manual_legs(environment: str, legs_data_list):
  """
    Checks if all legs in a list are valid tradable options.
    Returns True if all are valid, or an error string if one fails.
    """
  t, _ = server_helpers.get_tradier_client(environment)
  
  for leg in legs_data_list:
    # Build the OCC symbol just like your risk function does
    occ_symbol = server_helpers.build_occ_symbol(
      underlying=leg['underlying_symbol'], # You'll need to pass this in
      expiration_date=leg['expiration'],
      option_type=leg['option_type'],
      strike=leg['strike']
    )

    quote = server_helpers.get_quote(t, occ_symbol)

    if quote is None:
      return f"Invalid leg: {occ_symbol}"

  return True

@anvil.server.callable
def get_roll_package_dto(environment: str, 
                         trade_row: Row, 
                         margin_expansion_limit: float = 0
                        )->Dict:
  """
    Finds active legs, gets live prices, and calculates
    a full 4-leg roll package with standardized keys.
    It calls the main engine and returns a Dict with:
    {
    'legs_to_populate': None,
    'total_roll_credit': None,
    'new_spread_dto': best_position_object_dto,
    'closing_spread_dto': existing spread to close dto
    }
    'new_spread_dto' is a nested dict with {meta..., 'short_put', 'long_put'}
    """
  t, _ = server_helpers.get_tradier_client(environment)
  
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

    short_leg_quote = server_helpers.get_quote(t, short_occ)
    long_leg_quote = server_helpers.get_quote(t, long_occ)

    if not short_leg_quote or not long_leg_quote:
      raise Exception("Could not get live quotes for active legs.")

  except Exception as e:
    raise Exception(f"Error finding active legs: {e}")

    # --- 2. Calculate Closing Cost & Build Closing Leg Dicts ---
  current_spread = positions.DiagonalPutSpread(short_leg_quote, long_leg_quote)
  closing_spread_dto = current_spread.get_dto()
  closing_spread_dto['spread_action'] = config.TRADE_ACTION_CLOSE
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
  # calculate max roll to margin in dollars
  current_spread_reference_margin = server_helpers.calculate_reference_margin(trade_row, short_leg_db['Strike'], long_leg_db['Strike'])
  max_roll_to_margin = current_spread_reference_margin + margin_expansion_limit * short_leg_db['Quantity'] * config.DEFAULT_MULTIPLIER

  # convert roll to margin into roll to spread width
  max_roll_to_spread = math.ceil(max_roll_to_margin / (short_leg_db['Quantity'] * config.DEFAULT_MULTIPLIER))
  print(f" current_spread_reference_margin: {current_spread_reference_margin}")
  print(f" max_roll_to_margin: {max_roll_to_margin}, max roll to spread: {max_roll_to_spread}")
  
  new_spread_object = server_helpers.find_new_diagonal_trade(t, 
                                                             trade_row['Underlying'], 
                                                             current_spread, 
                                                             max_roll_to_spread)
  
  #print(f"new spread is: {new_spread}")
  if not new_spread_object or isinstance(new_spread_object, int):
    print(f"No valid roll configuration found for {trade_row['Underlying']}")
    return None
  # --- 4. Calculate Opening Credit & Build Opening Leg Dicts (FIXED) ---
  total_open_credit = new_spread_object.calculate_net_premium()
  #print(f"open credit of roll to: {total_open_credit}")
  
  # prepare for serialization
  new_spread_dto = new_spread_object.get_dto()
  new_short_leg_dto = new_spread_dto['short_put']
  new_long_leg_dto = new_spread_dto['long_put']
  new_spread_dto['spread_action'] = config.TRADE_ACTION_OPEN
  
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

  print(f"in get_roll: roll legs:{all_4_legs}, roll credit: {total_roll_credit}")

  return {
    'legs_to_populate': all_4_legs, # list of leg_dto [{leg1}, {leg2}, etc] closing-short, closing-long, opening-short, opening-long
    'total_roll_credit': total_roll_credit,
    'new_spread_dto': new_spread_dto,  # full nested { meta, 'short_put', 'long_put'} position dto
    'closing_spread_dto': closing_spread_dto # full nested { meta, 'short_put', 'long_put'} position dto
  }

@anvil.server.callable
def get_new_open_trade_dto(environment: str, symbol: str) -> Dict:
  """
    This is the new "wrapper" for the 'Find New Trade' button.
    It calls the main engine and returns a Dict with:
    {
    'legs_to_populate': None,
    'total_roll_credit': None,
    'new_spread_dto': best_position_object_dto
  }
    """
  t, _ = server_helpers.get_tradier_client(environment)
  # 1. Call your main engine (which is now a helper)
  # we get back a position object
  best_position_object = server_helpers.find_new_diagonal_trade(t,
                                                                underlying_symbol=symbol,
                                                                position_to_roll=None  # We pass None to trigger 'open' logic
  )
  
  # 2. Check the result
  if not best_position_object:
    print("find new diagonal trade did not return a best trade dto")
    return None # Or return an error string

  # 3. Convert the object to the spread DTO
  best_position_object_dto = best_position_object.get_dto()
  
  # mark as a spread open action
  best_position_object_dto['spread_action'] = config.TRADE_ACTION_OPEN
    
  return {
    'legs_to_populate': None,
    'total_roll_credit': None,
    'new_spread_dto': best_position_object_dto
  }

@anvil.server.callable
def delete_trade(trade_row):
  """
  Deletes a trade row and all associated transactions and legs.
  """
  if not trade_row:
    raise ValueError("No trade provided to delete")

  print(f"Deleting trade {trade_row.get_id()} and associated records...")

  try:
    # 1. Find all transactions for this trade
    transactions = app_tables.transactions.search(Trade=trade_row)

    # 2. For each transaction, delete its legs, then the transaction itself
    for t in transactions:
      # Delete legs associated with this transaction
      # Iterate and delete manually
      legs = app_tables.legs.search(Transaction=t)
      for leg in legs:
        leg.delete()
      # Delete the transaction
      t.delete()

    # 3. Finally, delete the trade row
    trade_row.delete()
    return "Trade deleted successfully."

  except Exception as e:
    print(f"Error deleting trade: {e}")
    raise e
    
@anvil.server.callable
def get_close_trade_dto(environment: str, trade_row: Row) -> Dict:
  """Calculates the closing trade package for an active position."""
  t, _ = server_helpers.get_tradier_client(environment)
  
  try:
    # 1. Get Active Legs from DB
    trade_transactions = app_tables.transactions.search(Trade=trade_row)
    short_leg_db = app_tables.legs.search(Transaction=q.any_of(*trade_transactions), active=True, Action='Sell to Open')[0]
    long_leg_db = app_tables.legs.search(Transaction=q.any_of(*trade_transactions), active=True, Action='Buy to Open')[0]

    # 2. Build OCC Symbols & Get Live Quotes
    short_occ = server_helpers.build_occ_symbol(trade_row['Underlying'], short_leg_db['Expiration'], short_leg_db['OptionType'], short_leg_db['Strike'])
    long_occ = server_helpers.build_occ_symbol(trade_row['Underlying'], long_leg_db['Expiration'], long_leg_db['OptionType'], long_leg_db['Strike'])

    short_quote = server_helpers.get_quote(t, short_occ)
    long_quote = server_helpers.get_quote(t, long_occ)

    # 3. Build DTO (Position Object)
    current_spread = positions.DiagonalPutSpread(short_quote, long_quote)
    close_dto = current_spread.get_dto()

    # Calculate Debit (Cost to Close)
    close_dto['cost_to_close'] = current_spread.calculate_cost_to_close()

    # mark as a spread closing action
    close_dto['spread_action'] = config.TRADE_ACTION_CLOSE

    return close_dto
  except Exception as e:
    print(f"Error getting close package: {e}")
    return None