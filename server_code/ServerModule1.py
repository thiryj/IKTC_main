import anvil.secrets
import anvil.server
from anvil.tables import app_tables
import datetime as dt

# Internal Libs
from shared import config
import positions
import server_helpers

# Helper classes
class MockOptionType:
  def __init__(self, name):
    self.name = name    # Allows .name access (fixes the crash)

  def __str__(self):
    return self.name    # Allows string comparison/printing

  def __eq__(self, other):
    # Allow comparison against string 'put' OR another Enum
    return self.name == str(other)
    
class ZombieQuote:
  """A safe default object for trades with missing/invalid API data."""
  def __init__(self, leg_data):
    self.symbol = leg_data.get('Symbol', 'UNKNOWN')
    self.bid = 0.0
    self.ask = 0.0
    self.last = 0.0
    self.strike = leg_data.get('Strike', 0.0)
    self.expiration_date = leg_data.get('Expiration', dt.date.today())
    self.contract_size = config.DEFAULT_MULTIPLIER
    self.option_type = config.OPTION_TYPE_PUT
    raw_type = leg_data.get('OptionType', 'put') # Default to put if missing
    self.option_type = MockOptionType(raw_type)

# ------------------------------------------------------------------
#  READ ACTIONS (UI Data Fetching)
# ------------------------------------------------------------------
@anvil.server.callable
def get_settings():
  """
    Returns app defaults to populate the UI.
    """
  return {
    'symbol': config.DEFAULT_SYMBOL,
    'quantity': config.DEFAULT_QUANTITY,
    'width': config.DEFAULT_WIDTH,
    'target_delta': config.DEFAULT_VERTICAL_DELTA,
    'harvest_target': config.DEFAULT_HARVEST_TARGET
  }

@anvil.server.callable
def get_new_open_trade_dto(target_dte=45, env=config.ENV_SANDBOX):
  """
    Finds a new opening position and converts it to a DTO for the UI.
    """
  t, _ = server_helpers.get_tradier_client(env) 

  # 1. Get the Logic Object
  spread_obj = server_helpers.get_vertical_spread(t, config.DEFAULT_SYMBOL, target_dte)

  # 2. Serialize for UI
  if spread_obj:
    return {
      'spread_dto': spread_obj.get_dto(),
      'target_date': spread_obj.short_put.expiration_date.strftime('%Y-%m-%d'),
      'message': 'Optimal vertical spread found.'
    }
  else:
    return {'message': 'No valid spread found matching criteria.'}

@anvil.server.callable
def get_roll_package_dto(trade_id):
  trade_row = app_tables.trades.get_by_id(trade_id)
  if not trade_row: return None

    # --- SANITY CHECK ---
  raw_env = trade_row['Account']
  trade_env = raw_env if raw_env in [config.ENV_PRODUCTION, config.ENV_SANDBOX] else config.ENV_SANDBOX
  t, _ = server_helpers.get_tradier_client(trade_env)

  # 1. Find Active Legs
  transactions = app_tables.transactions.search(Trade=trade_row)
  active_legs = []
  for txn in transactions:
    legs = app_tables.legs.search(Transaction=txn, active=True)
    active_legs.extend(legs)

  short_leg_row = next((leg for leg in active_legs if leg['Action'].startswith('Sell')), None)
  long_leg_row = next((leg for leg in active_legs if leg['Action'].startswith('Buy')), None)

  if not short_leg_row or not long_leg_row:
    return {'error': 'Database error: No active legs found.'}

    # 2. Convert to Dict & Quote (Current Position)
  short_leg_data = dict(short_leg_row)
  long_leg_data = dict(long_leg_row)
  
  short_quote = server_helpers.fetch_leg_quote(t, trade_row['Underlying'], short_leg_data)
  long_quote = server_helpers.fetch_leg_quote(t, trade_row['Underlying'], long_leg_data)
  print(f"short_leg_data: {short_leg_data}")
  print(f"short_quote: {short_quote}")

  # Zombie Patch
  if isinstance(short_quote, dict) or short_quote is None: short_quote = ZombieQuote(short_leg_data)
  if isinstance(long_quote, dict) or long_quote is None: long_quote = ZombieQuote(long_leg_data)

    # Fix Option Types if they are strings (Safety)
  if isinstance(short_quote.option_type, str): short_quote.option_type = MockOptionType(short_quote.option_type)
  if isinstance(long_quote.option_type, str): long_quote.option_type = MockOptionType(long_quote.option_type)

  current_pos = positions.PutSpread(short_quote, long_quote)
  print(f"current_pos: {current_pos.print_leg_details()}")

  # 3. Find New Position (Target)
  new_pos = server_helpers.find_vertical_roll(t, trade_row['Underlying'], current_pos)

  if not new_pos:
    return {'message': 'No valid zero-debit roll found.'}

    # 4. CONSTRUCT THE 4 LEGS (The "legs_to_populate" list)
    # We need to map Quote objects to the dict schema the UI grid expects.
    # IMPORTANT: "Closing" legs reverse the action (Sell->Buy, Buy->Sell).

  qty = int(short_leg_row['Quantity'])

  # Leg 1: Close the Old Short (Buy to Close)
  leg1 = {
    'symbol': short_quote.symbol,
    'quantity': qty,
    'action': config.ACTION_BUY_TO_CLOSE, 
    'option_symbol': short_quote.symbol, # Tradier quotes use symbol as the option identifier
    'strike': short_quote.strike,
    'expiration': short_quote.expiration_date
  }

  # Leg 2: Close the Old Long (Sell to Close)
  leg2 = {
    'symbol': long_quote.symbol,
    'quantity': qty,
    'action': config.ACTION_SELL_TO_CLOSE,
    'option_symbol': long_quote.symbol,
    'strike': long_quote.strike,
    'expiration': long_quote.expiration_date
  }

  # Leg 3: Open New Short (Sell to Open)
  leg3 = {
    'symbol': new_pos.short_put.symbol,
    'quantity': qty,
    'action': config.ACTION_SELL_TO_OPEN,
    'option_symbol': new_pos.short_put.symbol,
    'strike': new_pos.short_put.strike,
    'expiration': new_pos.short_put.expiration_date
  }

  # Leg 4: Open New Long (Buy to Open)
  leg4 = {
    'symbol': new_pos.long_put.symbol,
    'quantity': qty,
    'action': config.ACTION_BUY_TO_OPEN,
    'option_symbol': new_pos.long_put.symbol,
    'strike': new_pos.long_put.strike,
    'expiration': new_pos.long_put.expiration_date
  }

  all_4_legs = [leg1, leg2, leg3, leg4]

  closing_dto = current_pos.get_dto()
  closing_dto['cost_to_close'] = current_pos.calculate_cost_to_close()
  total_roll_credit = new_pos.net_premium - current_pos.calculate_cost_to_close()
  
  # 5. Return the EXACT Schema required
  return {
    'legs_to_populate': all_4_legs,
    'total_roll_credit': total_roll_credit,
    'new_spread_dto': new_pos.get_dto(),      # New Target
    'closing_spread_dto': closing_dto, # Old Position
    'spread_action': config.MANUAL_ENTRY_STATE_ROLL,
    'message': f"Roll found: Credit ${total_roll_credit:.2f}"
  }
# ------------------------------------------------------------------
#  WRITE ACTIONS (Execution & DB)
# ------------------------------------------------------------------

@anvil.server.callable
def submit_order(env, symbol, trade_dto_list, quantity, limit_price=None, preview=True):
  """
    Submits the order to Tradier.
    """
  t, url = server_helpers.get_tradier_client(env)
  
  if limit_price is not None:
    limit_price = abs(float(limit_price))

    # 2. Tradier rejects 0.00. If we calculated $0.00 (even), ask for $0.01.
    if limit_price < 0.01:
      print("Warning: Price was 0.00. Bumping to 0.01 to satisfy API.")
      limit_price = 0.01

  result = server_helpers.submit_spread_order(
    t, url, config.DEFAULT_SYMBOL, quantity, trade_dto_list, 
    preview=preview, limit_price=limit_price
  )
  return result

@anvil.server.callable
def save_trade_to_db(trade_dto, quantity):
  # 1. Create Trade
  trade_row = app_tables.trades.add_row(
    Symbol=config.DEFAULT_SYMBOL,
    Status=config.TRADE_ACTION_OPEN, 
    OpenedAt=dt.datetime.now(),
    Strategy=config.POSITION_TYPE_VERTICAL,
    Quantity=quantity
  )

  # 2. Create Transaction (Link to Trade)
  txn_row = app_tables.transactions.add_row(
    Trade=trade_row,
    Date=dt.datetime.now(),
    Type=config.TRADE_ACTION_OPEN,
    Description=f"Opening Vertical Spread {quantity}x"
  )

  # 3. Create Legs (Link to Transaction)
  short_leg_data = trade_dto['short_put']
  long_leg_data = trade_dto['long_put']

  app_tables.legs.add_row(
    Transaction=txn_row,
    Symbol=short_leg_data['symbol'],
    Strike=short_leg_data['strike'],
    Expiration=dt.datetime.strptime(short_leg_data['expiration_date'], "%Y-%m-%d").date(),
    OptionType=config.OPTION_TYPE_PUT,
    Side='Sell'
  )

  app_tables.legs.add_row(
    Transaction=txn_row,
    Symbol=long_leg_data['symbol'],
    Strike=long_leg_data['strike'],
    Expiration=dt.datetime.strptime(long_leg_data['expiration_date'], "%Y-%m-%d").date(),
    OptionType=config.OPTION_TYPE_PUT,
    Side='Buy'
  )

  return f"Trade saved with ID: {trade_row.get_id()}"

@anvil.server.callable
def get_open_trades_with_risk(env=config.ENV_SANDBOX):
  # MATCH SCHEMAS: Status='OPEN' AND Account matches env
  open_trades_rows = app_tables.trades.search(Status=config.TRADE_ACTION_OPEN, Account=env)
  t, _ = server_helpers.get_tradier_client(env)

  trade_list = []

  for trade_row in open_trades_rows:
    try:
      # 1. Find ALL Transactions for this trade
      transactions = app_tables.transactions.search(Trade=trade_row)
      if not any(transactions): continue

        # 2. Find ACTIVE Legs across ALL transactions
        # We don't care which transaction came last, we care which legs are active.
      active_legs = []
      for txn in transactions:
        # Assuming 'active' is a number (1) or boolean (True)
        # We search for active=1 based on your schema snippet.
        legs = app_tables.legs.search(Transaction=txn, active=True)
        active_legs.extend(legs)

        # Filter specifically for the Short and Long leg from the active bunch
      short_leg_row = next((l for l in active_legs if l['Action'].startswith('Sell')), None)
      long_leg_row = next((l for l in active_legs if l['Action'].startswith('Buy')), None)

      # If we rolled, the old legs should be active=0 and won't appear here.
      # If we don't find a pair, something is wrong with the 'active' flags.
      if not short_leg_row or not long_leg_row: 
        # Optional: Print warning if we have open trade but no active legs
        # print(f"Warning: Trade {trade_row.get_id()} is OPEN but has missing active legs.")
        continue

        # 3. CONVERT TO DICT & INJECT SYMBOL
      short_leg_data = dict(short_leg_row)
      short_leg_data['Symbol'] = trade_row['Underlying']

      long_leg_data = dict(long_leg_row)
      long_leg_data['Symbol'] = trade_row['Underlying']

      # 4. Get Quotes with SAFETY CHECKS
      short_quote = server_helpers.fetch_leg_quote(t, trade_row['Underlying'], short_leg_data)
      long_quote = server_helpers.fetch_leg_quote(t, trade_row['Underlying'], long_leg_data)

      # --- ZOMBIE PATCH START ---
      if isinstance(short_quote, dict) or short_quote is None:
        short_quote = ZombieQuote(short_leg_data)
      if isinstance(long_quote, dict) or long_quote is None:
        long_quote = ZombieQuote(long_leg_data)
        # --- ZOMBIE PATCH END ---

        # CHECK C: Handle None values in Bid/Ask
      if getattr(short_quote, 'bid', None) is None: short_quote.bid = 0.0
      if getattr(short_quote, 'ask', None) is None: short_quote.ask = 0.0
      if getattr(long_quote, 'bid', None) is None: long_quote.bid = 0.0
      if getattr(long_quote, 'ask', None) is None: long_quote.ask = 0.0

        # 5. Create Position Object & Calc Metrics
      pos = positions.PutSpread(short_quote, long_quote)
      cost_to_close = pos.calculate_cost_to_close()

      width = abs(short_leg_row['Strike'] - long_leg_row['Strike'])
      max_loss = width * 100

      # 6. Specific Metrics for UI
      underlying_price = short_quote.last if hasattr(short_quote, 'last') else 0
      intrinsic_val = max(0, short_leg_row['Strike'] - underlying_price)
      short_leg_price = (short_quote.bid + short_quote.ask) / 2
      extrinsic_val = max(0, short_leg_price - intrinsic_val)

      is_itm = underlying_price < short_leg_row['Strike']
      is_at_risk = is_itm and (extrinsic_val < 0.15)

      target_price = trade_row['TargetHarvestPrice'] or 0.0
      is_harvestable = (cost_to_close <= target_price) if target_price > 0 else False
      roi_display = f"{(cost_to_close / max_loss * 100):.1f}% Risk" if max_loss > 0 else "0%"

      # 7. BUILD THE UI-COMPATIBLE DICT
      trade_list.append({
        'trade_id': trade_row.get_id(),
        'trade_row': trade_row, 
        'Account': trade_row['Account'],

        'Underlying': trade_row['Underlying'],
        'Strategy': trade_row['Strategy'],
        'Quantity': f"{int(short_leg_row['Quantity'])}",
        'OpenDate': trade_row['OpenDate'],
        'short_expiry': short_leg_row['Expiration'],
        'short_strike': short_leg_row['Strike'],
        'long_strike': long_leg_row['Strike'],

        'harvest_price': f"${target_price:.2f}",
        'cost_to_close': cost_to_close,
        'rroc': roi_display,

        'extrinsic_value': extrinsic_val,
        'is_at_risk': is_at_risk,
        'is_harvestable': is_harvestable
      })

    except Exception as e:
      print(f"Error processing trade {trade_row.get_id()}: {e}")
      continue

  return trade_list
  
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
def get_price(symbol,env=config.ENV_SANDBOX):
  """Simple helper to get current stock price for the UI."""
  t, _ = server_helpers.get_tradier_client(env)
  try:
    quote = t.get_quotes(symbol)
    # return last price, or close if market is closed/pre-market
    return quote.get('last') or quote.get('close') or 0.0
  except Exception:
    return 0.0

import anvil.tables.query as q  # Ensure this is imported at the top

@anvil.server.callable
def get_active_legs_for_trade(trade_row, direction:str=None):
  """
    Finds all 'active' leg rows associated with a single trade.
    Args:
        trade_row: Row object or Trade ID string
        direction (str): 'short', 'long', or None (returns all)
    """
  # 1. Handle Input (Allow passing ID string or Row object)
  if isinstance(trade_row, str):
    trade_row = app_tables.trades.get_by_id(trade_row)

  if not trade_row: 
    return []

    # 2. Find all transactions for this trade
  trade_transactions = list(app_tables.transactions.search(Trade=trade_row))
  if not trade_transactions: 
    return []

    # 3. Build Query Filters
    # Base filter: Must belong to these transactions AND be active
  query_kwargs = {
    'Transaction': q.any_of(*trade_transactions),
    'active': True
  }

  # 4. Apply Direction Filter
  if direction:
    d = direction.lower()
    if d == 'short':
      # Robust match: matches "Sell to Open", "Sell To Open", etc.
      query_kwargs['Action'] = q.ilike('Sell%') 
    elif d == 'long':
      query_kwargs['Action'] = q.ilike('Buy%')

    # 5. Execute Search
  active_legs = app_tables.legs.search(**query_kwargs)

  return list(active_legs)