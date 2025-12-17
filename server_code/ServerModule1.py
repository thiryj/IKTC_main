# anvil section
import anvil.server
import anvil.secrets
import anvil.tables.query as q
from anvil.tables import app_tables, Row, order_by

# public lib sectoin
import math
from typing import Dict, Tuple, List
import datetime as dt
import json
from tradier_python import TradierAPI, Position

# personal lib section
import server_helpers
import positions
from shared import config

# To allow anvil.server.call() to call functions here, we mark
#  
@anvil.server.callable
def get_settings():
  settings_row = app_tables.settings.get()
  if not settings_row:
    settings_row = app_tables.settings.add_row(
      default_symbol=config.DEFAULT_SYMBOL,
      defualt_qty=1, 
      use_max_qty=False, 
      refresh_timer_on=True,
      allow_diagonals=False,
      margin_expansion_limit=0,
      default_width=config.DEFTAULT_WIDTH,
      harvest_fraction=config.DEFAULT_HARVEST_TARGET,
      automation_enabled=False
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
def get_open_trades_for_dropdown(environment: str=config.ENV_SANDBOX):
  """
    Fetches all open trades and formats them as a list
    of (display_text, item) tuples for a DropDown.
    """
  open_trades = app_tables.trades.search(
                                          Status=config.TRADE_ACTION_OPEN,
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
def get_open_trades_with_risk(environment: str=config.ENV_SANDBOX, 
                              refresh_risk: bool=True
                             )->List[Dict]:
  """
    Fetches all open trades, then enriches them with live
    pricing and assignment risk data from the Tradier API and RROC.
    """
  open_trades = app_tables.trades.search(Status=config.TRADE_ACTION_OPEN, Account=environment)
  #print(f"Found {len(open_trades)} open trades for {environment}")
  tradier_client, endpoint_url = server_helpers.get_tradier_client(environment)

  enriched_trades_list = []
  
  for trade in open_trades:
    trade_dto = {
      'trade_row': trade,
      'Underlying': trade['Underlying'],
      'Strategy': trade['Strategy'],
      'Quantity': None,
      'OpenDate': trade['OpenDate'],
      'short_strike': None,
      'long_strike': None,
      'short_expiry': None,
      'position_credit': None,
      'current_cost': None,
      'cumulative_credit': None,
      'rroc': "N/A",
      'harvest_price': "N/A",
      'is_harvestable': False,
      'roll_trigger': None
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
      if trade['Strategy'] in config.POSITION_TYPES_ACTIVE:
        #print(f"trade strategy: {trade['Strategy']} is in:{config.POSITION_TYPES_ACTIVE}")
        current_short_leg = next((leg for leg in active_legs if leg['Action'] == config.ACTION_SELL_TO_OPEN), None)
        if current_short_leg:
          #print(f"current_short_leg is: {current_short_leg}")
          quantity = current_short_leg['Quantity']
          trade_dto['Quantity'] = quantity
          trade_dto['short_strike'] = current_short_leg['Strike']
          trade_dto['short_expiry'] = current_short_leg['Expiration']
        else:
          print("missing short leg for the spread")
          continue
        
        current_long_leg =  next((leg for leg in active_legs if leg['Action'] == config.ACTION_BUY_TO_OPEN), None)
        if current_long_leg:
          trade_dto['long_strike'] = current_long_leg['Strike']
        else:
          print("missing long leg of the spread")
          continue
      else:
        print(f"trade strategy: {trade['Strategy']} is not in {config.POSITION_TYPES_ACTIVE}")
        
      if refresh_risk and current_short_leg:
        try:
          # A. Get Margin from latest transaction
          # Sort transactions by date to get the most recent margin entry
          sorted_trans = sorted(trade_transactions, key=lambda x: x['TransactionDate'], reverse=True)
          latest_open_transaction = next(
                                        (txn for txn in sorted_trans 
                                        if any(leg['Action'] in config.OPEN_ACTIONS for leg in app_tables.legs.search(Transaction=txn))),
                                          None)
          latest_margin = sorted_trans[-1]['ResultingMargin'] if sorted_trans else 0

          # B. Get Days in Trade
          days_in_trade = (dt.date.today() - trade['OpenDate']).days
          days_in_trade = 1 if days_in_trade < 1 else days_in_trade
          
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

          # E. RROC Calculation
          # Avoid division by zero
          if latest_margin and latest_margin > 0:
            daily_rroc = (current_pl_dollars / latest_margin) / days_in_trade
            trade_dto['rroc'] = daily_rroc 
          else:
            trade_dto['rroc'] = 0.0
          
          # Harvest Price = harvest fraction * credit price
          latest_position_credit = latest_open_transaction['CreditDebit']
          harvest_price = config.DEFAULT_HARVEST_TARGET * latest_position_credit
          trade_dto['harvest_price'] = harvest_price

          # Roll Trigger (3x Credit Limit)
          # "You must execute a defensive action immediately if... spread reaches 300% (3x)"
          trade_dto['roll_trigger'] = abs(latest_position_credit) * 3.0
          
          # flag if ready to harvest
          trade_dto['is_harvestable'] = True if cost_to_close_per_share <= harvest_price else False         

          trade_dto['position_credit'] = latest_position_credit
          trade_dto['cumulative_credit'] = total_credit_per_share
          trade_dto['current_cost'] = cost_to_close_per_share
          
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
def get_closed_trades(environment: str=config.ENV_SANDBOX, campaign_filter: str=None)->Dict: 
  search_kwargs = {'Campaign': campaign_filter} if campaign_filter else {}
  
  closed_trades = app_tables.trades.search(Status=config.TRADE_ACTION_CLOSE, Account=environment, **search_kwargs)
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
        if leg['Action'] in config.OPEN_ACTIONS:
          qty = leg['Quantity']
          break
    trade_dict['Quantity'] = qty
    
    # 2. Calc P/L & RROC
    pl = trade['TotalPL'] or 0.0
    total_pl_sum += pl

    # Trade level RROC
    if max_margin > 0:
      trade_daily_rroc = (pl / max_margin) / dit
      trade_dict['rroc'] = trade_daily_rroc
      trade_rroc_sum += trade_daily_rroc
      trade_count += 1

      # Portfolio level margin-days
      total_margin_days += (max_margin * dit)
    else:
      trade_dict['rroc'] = 0.0
  
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
  trade_response = server_helpers.submit_spread_order(t, 
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
  quantity = math.floor(config.ALLOCATION / best_position.margin) if best_position.margin > 0 else 0
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
def get_active_legs_for_trade(trade_row, direction:str=None):
  """
    Finds all 'active' leg rows associated with a single trade.
    Args:
    direction (str): 'short', 'long', or None (returns all)
    """
  try:
    # 1. Find all transactions for this trade
    trade_transactions = app_tables.transactions.search(Trade=trade_row)

    action_filter = None
    if direction:
      if direction.lower() == 'short':
        action_filter = config.ACTION_SELL_TO_OPEN
      elif direction.lower() == 'long':
        action_filter = config.ACTION_BUY_TO_OPEN
    if action_filter:    
      active_legs = app_tables.legs.search(
        Transaction=q.any_of(*trade_transactions),
        active=True, Action=action_filter
      )
    else:
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
def save_manual_trade(environment: str, 
                      strategy: str, #Strategy: Diagonal, Covered Call
                      manual_entry_state: str, # OPEN or CLOSE or ROLL 
                      trade_date, 
                      net_price: float, 
                      legs_data_list,            # list of leg data entries.  may be 1 (CSP) or 2 (diag open/close) or 4 (roll)                    
                      existing_trade_row=None,
                      open_spread_credit: float=None):  #CLOSE or ROLL: exising Trade row.  OPEN: None

  print(f"Server saving to environment {environment}: {strategy}")
  # --- 0. NEW: Safety Validation for Vertical Spreads ---
  if strategy == config.POSITION_TYPE_VERTICAL:
    try:
      # Filter for opening legs
      short_legs = [l for l in legs_data_list if l['action'] == config.ACTION_SELL_TO_OPEN]
      long_legs = [l for l in legs_data_list if l['action'] == config.ACTION_BUY_TO_OPEN]

      # If we have both (opening a new spread or rolling), check quantities
      if short_legs and long_legs:
        short_qty = short_legs[0]['quantity']
        long_qty = long_legs[0]['quantity']
        # calculate net credit here for harvest calculation
        
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
    settings_row = app_tables.settings.get()
    harvest_fraction = settings_row['harvest_fraction']
    # If it's a Roll but we have no DTO, we fallback to net_price (flawed but necessary fallback).
    basis_price = open_spread_credit if open_spread_credit is not None else net_price
    
    harvest_price = basis_price *  harvest_fraction if basis_price > 0 else 0
    # update trade row Status for CLOSE/ROLL  
    if manual_entry_state in (config.MANUAL_ENTRY_STATE_CLOSE, config.MANUAL_ENTRY_STATE_ROLL):
      trade_row = existing_trade_row
      if manual_entry_state == config.MANUAL_ENTRY_STATE_CLOSE:
        existing_trade_row.update(Status=config.TRADE_ACTION_CLOSE, CloseDate=trade_date)    
      elif manual_entry_state == config.MANUAL_ENTRY_STATE_ROLL:
        existing_trade_row.update(TargetHarvestPrice=harvest_price)
    # Create new trade row for OPEN
    elif manual_entry_state == config.MANUAL_ENTRY_STATE_OPEN:
      harvest_price = net_price * harvest_fraction
      trade_row = app_tables.trades.add_row(
        Underlying=underlying_symbol,
        Strategy=strategy,    # Strategy
        Status=config.TRADE_ACTION_OPEN,
        OpenDate=trade_date,
        Account=environment,
        Campaign=settings_row['current_campaign'],
        TargetHarvestPrice=harvest_price
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
        Status=config.TRADE_ACTION_CLOSE, 
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
                         margin_expansion_limit: float = config.LONG_STRIKE_DELTA_MAX
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

  # --- NEW: Calculate Total Original Credit ---
  # Sum of all credits collected so far (Open + previous Rolls)
  # We use this to determine the "10% Pain Threshold"
  trade_transactions = app_tables.transactions.search(Trade=trade_row)
  original_credit = sum(
    t['CreditDebit'] 
    for t in trade_transactions 
    if t['CreditDebit'] is not None
  )
  
    # --- 2. Calculate Closing Cost & Build Closing Leg Dicts ---
  current_spread = positions.DiagonalPutSpread(short_leg_quote, long_leg_quote)
  closing_spread_dto = current_spread.get_dto()
  closing_spread_dto['spread_action'] = config.TRADE_ACTION_CLOSE
  total_close_cost = current_spread.calculate_cost_to_close()

  # Build standardized dicts for the closing legs
  closing_leg_1 = {
    'action': config.ACTION_BUY_TO_CLOSE,
    'type': short_leg_db['OptionType'],
    'strike': short_leg_db['Strike'],
    'expiration': short_leg_db['Expiration'],
    'quantity': short_leg_db['Quantity']
  }
  closing_leg_2 = {
    'action': config.ACTION_SELL_TO_CLOSE,
    'type': long_leg_db['OptionType'],
    'strike': long_leg_db['Strike'],
    'expiration': long_leg_db['Expiration'],
    'quantity': long_leg_db['Quantity']
  }
  closing_legs_list = [closing_leg_1, closing_leg_2]

  # --- 3.  Find NEW Legs ---
  limit_ticks = int(margin_expansion_limit)
  new_spread_object = server_helpers.find_vertical_roll(
    t,
    trade_row['Underlying'],
    current_spread,
    original_credit=original_credit,
    margin_expansion_limit_ticks=limit_ticks
  )
  
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
    'action': config.ACTION_SELL_TO_OPEN,
    'type': new_short_leg_dto['option_type'],
    'strike': new_short_leg_dto['strike'],
    'expiration': new_short_leg_dto['expiration_date'],
    'quantity': 1 # Assuming quantity 1
  }
  opening_leg_2 = {
    'action': config.ACTION_BUY_TO_OPEN,
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
  print(f" in get_roll: new_spread_dto['net_premium']: {new_spread_dto['net_premium']}")
  return {
    'legs_to_populate': all_4_legs, # list of leg_dto [{leg1}, {leg2}, etc] closing-short, closing-long, opening-short, opening-long
    'total_roll_credit': total_roll_credit,
    'new_spread_dto': new_spread_dto,  # full nested { meta, 'short_put', 'long_put'} position dto
    'closing_spread_dto': closing_spread_dto # full nested { meta, 'short_put', 'long_put'} position dto
  }

@anvil.server.callable
def get_new_open_trade_dto(environment: str, 
                           symbol: str=None, 
                           strategy_type: str=None
                          ) -> Dict:
  """
  Unified wrapper for 'Find New Trade'.
  Dispatches to the find new Vertical and normalizes the output 
  so the UI receives a consistent DTO with:
    {
    'legs_to_populate': None,
    'total_roll_credit': None,
    'new_spread_dto': best_position_object_dto
  }
    """
  t, _ = server_helpers.get_tradier_client(environment)

  # 1. Resolve Settings
  settings_row = app_tables.settings.get() or {} # Assumes single-row settings table
  width = settings_row['default_width'] if settings_row and settings_row['default_width'] else config.DEFAULT_WIDTH
  qty = settings_row['default_qty'] if settings_row and settings_row['default_qty'] else config.DEFAULT_QUANTITY
  
  best_spread_object = None
  if strategy_type == config.POSITION_TYPE_VERTICAL:
    # This helper returns a Dictionary result that now contains a position object in the 'legs' key
    result = server_helpers.get_vertical_spread(t, 
                                                symbol=symbol, 
                                                target_delta=config.DEFAULT_VERTICAL_DELTA, 
                                                width=width, 
                                                quantity=qty
                                                )
    if result and not result.get('error'):
      #print(f"get_new_open_trade_dto.  result: {result}")
      best_spread_object = result['legs']
      qty = result['parameters']['quantity'] # Capture quantity before discarding result dict
  else:
    anvil.alert(f"Strategy: {strategy_type} is not implemented")
    return
  
  if not best_spread_object:
    print(f"find new trade for {strategy_type} on {symbol} did not return a best trade object")
    return None # Or return an error string
    
  best_spread_dto = best_spread_object.get_dto()
  # inject qty into return and mark as a spread open action
  best_spread_dto['quantity'] = qty
  best_spread_dto['spread_action'] = config.TRADE_ACTION_OPEN
    
  return {
    'legs_to_populate': None,
    'total_roll_credit': None,
    'new_spread_dto': best_spread_dto
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

@anvil.server.callable
def get_price(environment: str, symbol: str, price_type: str=None)->float:
  t, _ = server_helpers.get_tradier_client(environment)
  return server_helpers.get_underlying_price(t, symbol)

@anvil.server.callable
def set_automation_status(enabled: bool):
  app_tables.settings.get().update(automation_enabled=enabled)

@anvil.server.callable
def is_automation_live():
  # The Headless Bot calls this first. If False, it terminates immediately.
  return app_tables.settings.get()['automation_enabled']

@anvil.server.callable
def log_automation_event(environment: str, level: str, source: str, message: str, data: dict = None):
  """
  Writes a permanent record to the logs.
  Call this whenever the bot makes a decision or hits an error.
  """
  # Optional: Print to console for real-time debugging while you watch
  print(f"[{level}] {source}: {message}")

  # Write to DB
  app_tables.automationlogs.add_row(
    timestamp=dt.datetime.now(),
    level=level,
    source=source,
    message=message,
    data=data,
    environment=environment
  )

  # Cleanup (Optional): Keep table size manageable
  # You might want a separate scheduled task to delete logs older than 30 days

@anvil.server.callable
def get_recent_logs(environment:str, limit:int=50)->List[Dict]:
  # Return sorted by newest first
  recent_logs = app_tables.automationlogs.search(
    order_by("timestamp", ascending=False),
    environment=environment
  )[:limit]
  return [dict(r) for r in recent_logs]

@anvil.server.callable
def log_test():
  trade_row = app_tables.trades.search()[0]
  current_price=3
  limit_price=2
  log_automation_event(
                    level="ACTION", 
                    source="RollLogic", 
                    message=f"Triggering defensive roll. Price {current_price} exceeded limit {limit_price}",
                    data={'trade_id': trade_row.get_id(), 'ask': current_price, 'limit': limit_price}
                  )

@anvil.server.background_task
@anvil.server.callable
def run_automation_cycle():
  """
  The Heartbeat. Runs every X minutes via Anvil Scheduled Tasks.
  Phase 1: Scan
  Phase 2: Decide  - Execute and log
  """
  # 1. Global Kill Switch
  settings = app_tables.settings.get()
  if not settings['automation_enabled']:
    print("Automation is disabled. Skipping cycle.")
    return

  # 2. Setup Environment (Default to Sandbox for safety)
  env = config.ENV_SANDBOX
  print(f"Starting automation cycle for {env}...")
  log_automation_event(
    level="INFO", 
    source="Scheduler", 
    message=f"Starting automation cycle for {env}...", 
    environment=env
  )

  # 3. SCAN: Get live data
  try:
    active_trades = get_open_trades_with_risk(env, refresh_risk=True)
  except Exception as e:
    log_automation_event("ERROR", "Scanner", f"Failed to scan trades: {e}", env)
    return

  # 4. DECISION ENGINE
  for trade in active_trades:
    try:
      symbol = trade['Underlying']
  
      # Skip invalid data (e.g. if API failed for this row)
      if trade.get('current_cost') is None or trade.get('position_credit') is None:
        continue
  
      current_cost = trade['current_cost']
      position_credit = trade['position_credit']
      stop_loss_price = abs(position_credit) * 3.0

      # --- RULE 1: DEFENSIVE ROLL (3x Credit Stop) ---
      # We check Defense FIRST. If a trade is in trouble, we fix it before looking for profit.
      if abs(current_cost) >= stop_loss_price:
        message = f"DEFENSE TRIGGER: {symbol} hit 3x Stop. Cost: {current_cost:.2f} >= Limit: {stop_loss_price:.2f}"
        log_automation_event("WARNING", "RiskManager", message, env, data={'trade_id': trade['trade_row'].get_id()})

        # A. Calculate the Roll Package
        roll_package = get_roll_package_dto(env, trade['trade_row'])

        if roll_package:
          # B. Submit the 4-Leg Order
          # Note: 'legs_to_populate' is the list of 4 dicts that submit_order needs
          response = submit_order(
            environment=env,
            underlying_symbol=symbol,
            trade_dto_list=roll_package['legs_to_populate'], 
            quantity=trade['Quantity'],
            preview=False,
            limit_price=roll_package['total_roll_credit'] # Can be positive (Credit) or negative (Debit)
          )

          if response and response.get('order', {}).get('status') == 'ok':
            order_id = response['order']['id']
            log_automation_event("INFO", "RiskManager", f"Roll Order {order_id} Submitted.", env)
            continue # Stop processing this trade (don't harvest if we just rolled)
          else:
            err = response.get('order', {}).get('errors') if response else "Unknown Error"
            log_automation_event("ERROR", "RiskManager", f"Roll Order Failed: {err}", env)
        else:
          log_automation_event("ERROR", "RiskManager", f"Could not calculate valid roll for {symbol}", env)
      # --- RULE 2: PROFIT TAKING (50% of Premium) ---
      # Only check harvest if we aren't rolling
      elif trade.get('is_harvestable'):
        message = f"HARVEST TRIGGER: {symbol} Profit Target Hit. Credit: {position_credit:.2f}, Cost: {current_cost:.2f}"
        log_automation_event("ACTION", "Harvester", message, env, data={'trade_id': trade['trade_row'].get_id()})
  
        close_dto = get_close_trade_dto(env, trade['trade_row'])

        if close_dto:
          # Note: submit_order expects a LIST of DTOs
          response = submit_order(
            environment=env,
            underlying_symbol=symbol,
            trade_dto_list=[close_dto], 
            quantity=trade['Quantity'],
            preview=False, # <--- FIRE FOR EFFECT
            limit_price=trade['harvest_price']
          )

          # 5. Log the Result
          if response and response.get('order', {}).get('status') == 'ok':
            order_id = response['order']['id']
            log_automation_event("INFO", "Harvester", f"Harvest Order {order_id} Submitted.", env)
          else:
            err = response.get('order', {}).get('errors') if response else "Unknown Error"
            log_automation_event("ERROR", "Harvester", f"Order Submission Failed: {err}", env)

    except Exception as e:
      log_automation_event("ERROR", "Harvester", f"Crash during harvest execution: {e}", env)
