# Anvil libs
import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

# Public libs
import requests
import math
from urllib.parse import urljoin
from typing import Dict, List, Tuple
from datetime import date, datetime
from pydantic_core import ValidationError
from tradier_python import TradierAPI
from tradier_python.models import Quote

# Private libs
import server_config
import config #client side / combined config
import positions
import ServerModule1

# This is a server module. It runs on the Anvil server,
# rather than in the user's browser.
#
# To allow anvil.server.call() to call functions here, we mark
# them with @anvil.server.callable.
# Here is an example - you can replace it with your own:
#
# @anvil.server.callable

def get_tradier_client(environment: str)->Tuple[TradierAPI, str]:
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
  return t, endpoint_url

def get_quote(environment: str, symbol: str) ->str:
  # get full quote data for a single symbol
  t, endpoint_url = get_tradier_client(environment)
  try:
    quote_list = t.get_quotes([symbol, "bogus"], greeks=False)
    # note:  needed to send a fake symbol in because of a bug in the get_quotes endpoint
    if quote_list:
      return quote_list[0]
    else:
      return None
  except Exception as e:
    print(f"Validation failed for symbol {symbol}: {e}")
    return None
    
def get_near_term_expirations(tradier_client: TradierAPI, 
                              symbol: str, 
                              max_days_out: int = 10
                             ) -> List[date]:
  """
    Fetches all option expiration dates for a symbol and filters for near-term dates.

    Args:
        tradier_client (TradierAPI): An initialized tradier-python client object.
        symbol (str): The underlying stock symbol (e.g., "IWM").
        max_days_out (int, optional): The maximum number of days from today
                                      to include. Defaults to 30.

    Returns:
        List[date]: A list of datetime.date objects representing the valid,
                    near-term expiration dates.
    """
  # 1. Fetch all valid expiration dates from the API
  all_expirations = tradier_client.get_option_expirations(symbol=symbol, include_all_roots=True)

  # 2. Get today's date for comparison
  today = date.today()

  # 3. Use a list comprehension to filter for dates within the desired window
  near_term_expirations = [
    exp for exp in all_expirations
    if 0 <= (exp - today).days <= max_days_out
  ]

  return near_term_expirations

def get_valid_diagonal_put_spreads(short_strike: float,
                                   tradier_client: TradierAPI,
                                   symbol: str,
                                   max_days_out: int = 10,
                                   short_expiry: date = None,
                                   max_spread_width: float = server_config.LONG_STRIKE_DELTA_MAX
                                  )->List[positions.DiagonalPutSpread]:
  print(f"get_valid: short strike:{short_strike}, symbol: {symbol}, short expiry: {short_expiry}, max_spread_width: {max_spread_width}")
  # get list of valid expirations
  expirations = get_near_term_expirations(tradier_client=tradier_client, symbol=symbol, max_days_out=max_days_out)
  # if roll, exclude all expirations equal to or before existing short expiry.  if not roll, then short_expiry = today
  expirations = expirations if short_expiry is None else [expiry for expiry in expirations if expiry > short_expiry]
  exp_count = len(expirations)
  valid_positions = []
  #print(f"expirations: {expirations}")
  for i in range(3):
    short_put_expiration = expirations[i]
    for j in range(i+1, exp_count):
      long_put_expiration = expirations[j]

      # grab the chains for this valid pair of short put + long put
      try:
        short_put_chain = tradier_client.get_option_chains(symbol=symbol, expiration=short_put_expiration.strftime('%Y-%m-%d'), greeks=False)
      except ValidationError as e:
        print(f"get short option chain: {e}")
        continue
      try:
        long_put_chain = tradier_client.get_option_chains(symbol=symbol, expiration=long_put_expiration.strftime('%Y-%m-%d'), greeks=False)
      except ValidationError as e:
        print(f"get long option chain: {e}")
        continue

      # for a valid expiration pair, iterate through long put strikes
      for k in range(1, max_spread_width):

        # build the position 
        short_puts = [
          opt for opt in short_put_chain
          if opt.option_type == 'put'
          and opt.strike == short_strike
        ]
        if not short_puts:
          continue
        long_puts = [
          opt for opt in long_put_chain
          if opt.option_type == 'put'
          and opt.strike == short_strike - k
        ]
        if not long_puts:
          continue
        new_position = positions.DiagonalPutSpread(short_puts[0], long_puts[0])
        valid_positions.append(new_position)
  return valid_positions
  
def submit_diagonal_spread_order(
  environment: str,
  tradier_client: TradierAPI,
  endpoint_url: str,
  underlying_symbol: str,
  quantity: int,
  trade_dto_list: List, # list of dicts with {spread meta..., 'short_put', 'long_put'}
  preview: bool = True,
  limit_price: float = None
) -> Dict:
  """
  Submits a multi-leg option order directly using the session,
  bypassing the buggy library helper functions. Can be used for previews.

  Args:
      tradier_client (TradierAPI): The initialized API client.
      endpoint_url (str): The endpoint url
      underlying_symbol (str)
      quantity (int): The number of contracts for each leg.
      trade_dto_list: # list of dicts with {spread meta..., 'short_put', 'long_put'}
      preview (bool, optional): If True, submits as a preview order.
                                Defaults to False.

  Returns:
      Dict: The JSON response from the API as a dictionary, or None if an
            error occurred.
  """
  #api_url = f"{endpoint_url}/accounts/{tradier_client.default_account_id}/orders"
  path = f"accounts/{tradier_client.default_account_id}/orders"
  #print(f"endpoint_url: {endpoint_url}")
  api_url = urljoin(endpoint_url, path)

  payload = build_multileg_payload(environment, underlying_symbol, quantity, trade_dto_list)

  # override price if limit_price sent in
  if limit_price is not None:
    payload['price'] = f"{limit_price:.2f}"

  # Conditionally add the 'preview' or 'type' parameter based on the flag
  if preview:
    payload['preview'] = 'true'

  try:
    #print(f"payload is: {payload}")
    #print(f"api_url is: {api_url}")
    response = tradier_client.session.post(api_url, data=payload, headers={'accept': 'application/json'})
    #print(f"response is: {response.text}")
    response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
    return response.json()
  except requests.exceptions.HTTPError as e:  # <-- Catch the specific HTTP error
    print(f"An HTTP error occurred submitting the order: {e}")
    # --- THIS IS THE KEY ---
    # Print the detailed error message from the API response body
    print(f"API Response Details: {e.response.text}")
    return None
  except Exception as e:
    print(f"An error occurred submitting the order: {e}")
    return None

def build_multileg_payload(
  environment: str,
  underlying_symbol: str, 
  quantity: int,
  trade_dto_list: List # list of nested dicts with {spread meta..., 'short_put', 'long_put'}
)->Dict:
  """
    Builds the API payload for a multileg order from a list of spreads.
    - A list with 1 spread is treated as an 'open' [open].
    - A list with 2 spreads is treated as a 'roll' [open, close].
    """
  legs = []
  # --- Build the common payload keys --- don't change the key names, they are set by API
  payload = {
    'class': 'multileg',
    'symbol': underlying_symbol,
    'duration': 'day',
    'type': 'credit',
  }

  print(f"trade_dto_list is: {trade_dto_list}")
  # extract legs from nested dict and create position objects
  #short_put_dto = trade_dto.get('short_put')
  #long_put_dto = trade_dto.get('long_put')
  
  position_to_open_dto = trade_dto_list[0]
  legs.append({'symbol': position_to_open_dto.get('short_put',{}).get('symbol'), 'side': 'sell_to_open'})
  legs.append({'symbol': position_to_open_dto.get('long_put', {}).get('symbol'), 'side': 'buy_to_open'})
  
  if len(trade_dto_list) == 1:  # this is an open
    payload['price'] = f"{position_to_open_dto.get('net_premium'):.2f}"

    # --- Case 2: Roll a 4-leg position ---
    # TODO:  following line is a hack.  need to deal with 4 legged trade_dto
  elif len(trade_dto_list) == 2:
    # Convention: The first position is to open, the second is to close.
    position_to_close_dto = trade_dto_list[1]
    short_position_to_close_symbol = position_to_close_dto.get('short_put',{}).get('symbol')
    long_position_to_close_symbol = position_to_close_dto.get('long_put',  {}).get('symbol')
    # Add legs to CLOSE the existing position
    legs.append({'symbol': short_position_to_close_symbol, 'side': 'buy_to_close'})
    legs.append({'symbol': long_position_to_close_symbol, 'side': 'sell_to_close'})
    
    credit_to_open = position_to_open_dto.get('net_premium')

    #print(f"build multi leg: credit to open: {credit_to_open}")
    # get fresh cost to close
    short_position_to_close_quote = get_quote(environment, short_position_to_close_symbol)
    long_position_to_close_quote =  get_quote(environment, long_position_to_close_symbol)
    cost_to_close = short_position_to_close_quote.ask - long_position_to_close_quote.bid
    roll_value = credit_to_open - cost_to_close
    #print(f"build multi leg: roll credit: {roll_value}")
    payload['price'] = f"{abs(roll_value):.2f}"
    payload['type'] = 'credit' if roll_value >= 0 else 'debit'

  else:
    # Handle invalid input
    print("Error: trade_list must contain 1 or 2 positions.")
    return None

  # Dynamically add each leg and its quantity to the payload
  for i, leg in enumerate(legs):
    payload[f'option_symbol[{i}]'] = leg['symbol']
    payload[f'side[{i}]'] = leg['side']
    payload[f'quantity[{i}]'] = quantity # Assumes same quantity for all new/closing legs
    
  print(f"build_multi_leg_payload: payload: {payload}")
  return payload

def get_strike(symbol:str) -> float:
  """
    Extracts the strike price from an OCC option symbol and returns it as a float.
    """
  strike_price = int(symbol[-8:]) / 1000
  return strike_price

def get_expiration_date(symbol: str) -> date | None:
  """
    Extracts the expiration date from an OCC option symbol and returns it as a date object.
    """
  try:
    # The date is always the 6 characters before the last 9 characters (type + strike)
    date_str = symbol[-15:-9]
    # '%y%m%d' tells strptime to parse a 2-digit year, month, and day
    return datetime.strptime(date_str, '%y%m%d').date()
  except (ValueError, IndexError):
    # Handles cases where the symbol is malformed or too short
    return None

def build_occ_symbol(underlying, expiration_date, option_type, strike):
  """
    Builds a 21-character OCC option symbol from its parts.
    e.g., IWM, 2025-11-03, Put, 247 -> IWM251103P00247000
    """
  # Format date to YYMMDD (e.g., '251103')
  exp_str = expiration_date.strftime('%y%m%d')

  # Format type to P or C
  type_char = 'P' if option_type.upper() == config.OPTION_TYPE_PUT else 'C'
  
  # Format strike to 8-digit string, (e.g., 247 -> '00247000')
  # This assumes strike is a number. We multiply by 1000 and pad with zeros.
  strike_int = int(strike * 1000)
  strike_str = f"{strike_int:08d}"

  # Combine all parts
  return f"{underlying}{exp_str}{type_char}{strike_str}"

def get_net_roll_rom_per_day(pos: positions.DiagonalPutSpread, cost_to_close: float, today: date)-> float:
  dte = (pos.short_put.expiration_date - today).days

  # Check for DTE and valid margin
  if dte <= 0 or not pos.margin or pos.margin <= 0:
    return -float('inf')

  net_roll_credit = pos.net_premium - cost_to_close
  return_on_margin = net_roll_credit / pos.margin 

  return return_on_margin / dte

def find_new_diagonal_trade(environment: str='SANDBOX', 
                            underlying_symbol: str=None,
                            position_to_roll: positions.DiagonalPutSpread=None,
                            max_roll_to_margin: float = server_config.LONG_STRIKE_DELTA_MAX
                           )->positions.DiagonalPutSpread:
  """
  Connect to Tradier,
  find an optimal new short put diagonal, and return a position object.
  if an existing position is passed in, then use roll logic,otherwise its a simple open
  """
  print("Server function 'find_new_diagonal_trade' was called.")
  if underlying_symbol is None:
    anvil.alert("must select underlying symbol")
    return

  t, endpoint_url = get_tradier_client(environment)

  roll = True if position_to_roll else None

  if roll is None: # use simple open logic
    print("simple open")
    underlying_price = ServerModule1.get_underlying_quote(environment, underlying_symbol)
    short_strike = math.ceil(underlying_price)
    short_expiry = None
  else:     # use roll logic
    short_symbol = position_to_roll.short_put.symbol
    #print(f"position to roll short symbol: {short_symbol}")
    short_strike = get_strike(short_symbol)
    short_expiry = get_expiration_date(short_symbol)
    short_quote = t.get_quotes([short_symbol, "bogus"],greeks=False)[0]
    long_symbol = position_to_roll.long_put.symbol
    long_quote = t.get_quotes([long_symbol, "bogus"], greeks=False)[0]

    live_position_to_roll = positions.DiagonalPutSpread(short_quote, long_quote)
    #cost_to_close = short_quote.ask = long_quote.bid
    cost_to_close = live_position_to_roll.calculate_cost_to_close()

  # get list of valid positions
  print("calling get_valid_diagonal_put_spreads")
  valid_positions = get_valid_diagonal_put_spreads(short_strike=short_strike, 
                                                                  tradier_client=t, 
                                                                  symbol=underlying_symbol, 
                                                                  max_days_out=server_config.MAX_DTE,
                                                                  short_expiry=short_expiry,
                                                                  max_spread_width = max_roll_to_margin
                                                  )
  number_positions = len(valid_positions)
  print(f"Number of valid positions: {len(valid_positions)}")
  if number_positions == 0:
    print("Halting script - no positions")
    return "no positions"

  if roll:
    # positions must have a larger credit to open than the existing spread cost to close    
    valid_positions = [pos for pos in valid_positions if 
                       (pos.net_premium > 0.01 and #abs(cost_to_close) and 
                        pos.short_put.expiration_date >= short_quote.expiration_date and
                        pos.long_put.expiration_date != long_quote.expiration_date
                       )
                      ]

    # find best put diag based on highest return on margin per day of trade
    today = date.today()
    sorted_positions = sorted(
      valid_positions,
      key=lambda pos: get_net_roll_rom_per_day(pos, cost_to_close, today),
      reverse=True
    )
    best_position = sorted_positions[0] if sorted_positions else None
  else:
    best_position = max(
      valid_positions,
      key=lambda pos: pos.ROM_rate,
      default=None
    )
  if not best_position:
    print("No good position identified")
    return 1

  print('To Open')
  best_position.print_leg_details()
  best_position.describe()
  # best_position is a position object
  return best_position

def build_leg_dto(spread_dto: Dict, option_index)->Dict:
  """
  takes a spread dto and return a leg dto
  """
  selected_leg = spread_dto[option_index]
  leg_dto = {
    'action': 'Sell to Open',
    'type': selected_leg['option_type'],
    'strike': selected_leg['strike'],
    'expiration': selected_leg['expiration_date'],
    'quantity': None # filled in later
  }
  return leg_dto

"""
opening_leg_1 = {
    'action': 'Sell to Open',
    'type': new_short_leg_dto['option_type'],
    'strike': new_short_leg_dto['strike'],
    'expiration': new_short_leg_dto['expiration_date'],
    'quantity': 1 # Assuming quantity 1
  }
  """
# In server_helpers.py
# Make sure app_tables is available (from anvil.tables import app_tables)

def calculate_reference_margin(trade_row, 
                               current_short_strike: float, 
                               current_long_strike: float
                              ) -> float:
  """
    Calculates the true margin requirement of the current open spread by
    subtracting cumulative collected credits from the maximum loss.

    Args:
        trade_row: The Trades table Row object being analyzed.
        current_short_strike (float): The strike of the currently active short leg.
        current_long_strike (float): The strike of the currently active long leg.

    Returns:
        float: The calculated margin requirement (the capital at risk).
    """

  # 1. Get all transactions linked to this trade.
  # This includes the Open transaction and all prior Roll transactions.
  all_transactions = app_tables.transactions.search(Trade=trade_row)

  # 2. Calculate the Cumulative Net Credit/Debit
  # This is the total cash buffer you have received.
  cumulative_credit = sum(
    t['CreditDebit'] 
    for t in all_transactions 
    if t['CreditDebit'] is not None
  )

  # 3. Calculate the Max Loss of the CURRENT spread
  # Max Loss = Spread Width * Multiplier (100)
  spread_width = abs(current_short_strike - current_long_strike)
  initial_max_loss = spread_width * 100

  # 4. Calculate the Reference Margin (what the broker holds)
  # This is the capital at risk.
  reference_margin = initial_max_loss - cumulative_credit

  # 5. Safety check: Margin cannot be less than zero
  return max(0, reference_margin)