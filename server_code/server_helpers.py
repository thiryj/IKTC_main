# Anvil libs
import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

# Public libs
import requests
import math
import logging
import sys
from urllib.parse import urljoin
from typing import Dict, List, Tuple, Any, TYPE_CHECKING
import datetime as dt
from pydantic_core import ValidationError
from tradier_python import TradierAPI
from tradier_python.models import Quote
from itertools import groupby

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
    
def get_near_term_expirations(tradier_client: TradierAPI, 
                              symbol: str, 
                              max_days_out: int = 10
                             ) -> List[dt.date]:
  """
    Fetches all option expiration dates for a symbol and filters for near-term dates.

    Args:
        tradier_client (TradierAPI): An initialized tradier-python client object.
        symbol (str): The underlying stock symbol (e.g., "IWM").
        max_days_out (int, optional): The maximum number of days from today
                                      to include. Defaults to 30.

    Returns:
        List[dt.date]: A list of datetime.date objects representing the valid,
                    near-term expiration dates.
    """
  # 1. Fetch all valid expiration dates from the API
  all_expirations = tradier_client.get_option_expirations(symbol=symbol, include_all_roots=True)

  # 2. Get today's date for comparison
  today = dt.date.today()

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
                                   short_expiry: dt.date = None,
                                   max_spread_width: int = server_config.LONG_STRIKE_DELTA_MAX
                                  )->List[positions.DiagonalPutSpread]:
  print(f"get_valid: short strike:{short_strike}, symbol: {symbol}, short expiry: {short_expiry}, max_spread_width: {max_spread_width}")
  # get list of valid expirations
  expirations = get_near_term_expirations(tradier_client=tradier_client, symbol=symbol, max_days_out=max_days_out)
  # if roll, exclude all expirations equal to or before existing short expiry.  if not roll, then short_expiry = today
  expirations = expirations if short_expiry is None else [expiry for expiry in expirations if expiry > short_expiry]
  exp_count = len(expirations)
  valid_positions = []
  #print(f"expirations: {expirations}")
  
  for i in range(min(config.ROLL_EXPIRATION_EXTENSION_LIMIT, exp_count)): #this is the short expiry outer loop.  5 means don't look at short strikes further than 5 expiries out from today
    short_put_expiration = expirations[i]
    short_put_chain = fetch_option_chain_direct(tradier_client=tradier_client,
                                                symbol=symbol,
                                                expiration=short_put_expiration
    )
    if not short_put_chain:  continue
    # Filter for the matching *dictionary* for the short put
    short_puts_data = [
      opt_data for opt_data in short_put_chain
      if opt_data['option_type'] == 'put'
      and opt_data['strike'] == short_strike
    ]
    if not short_puts_data: continue
    #print(f"short_puts_data[0]: {short_puts_data[0]}")
    try:
      short_put_obj = Quote(**short_puts_data[0])
    except (TypeError, KeyError, ValidationError) as e:
      print(f"Bad data for short leg {symbol} date {short_put_expiration}: {e}")
      continue

    long_dte_offset = 0 # 0 to allow verticals.  1 or more to force diagonals
    # use for j in range(i, i+1) if I want to enforce only verticals
    end_range = i+1 if config.VERTICAL_SPREADS_ONLY else exp_count     # counting j up to exp_count considers diagonals
    for j in range(i+long_dte_offset, end_range):
      long_put_expiration = expirations[j]
      #long_put_chain = tradier_client.get_option_chains(symbol=symbol, expiration=long_put_expiration.strftime('%Y-%m-%d'), greeks=False)
      long_put_chain = fetch_option_chain_direct(
                                                tradier_client=tradier_client,
                                                symbol=symbol,
                                                expiration=long_put_expiration
      )
      if not long_put_chain: continue
      # for a valid expiration pair, iterate through long put strikes
      for k in range(1, max_spread_width + 1):

        # build the long position 
        long_puts_data = [
          opt_data for opt_data in long_put_chain
          if opt_data['option_type'] == 'put'
        ]
        # Sort descending (High to Low) so index+1 is the next strike DOWN
        sorted_long_puts = sorted(long_puts_data, key=lambda x: x['strike'], reverse=True)
        all_strikes = [opt['strike'] for opt in sorted_long_puts]

        # B. Find where the Short Strike 'sits' in this chain.  can't use simple .index because floating point uncertainty: 1.0 <> 1.00000001
        try:
          start_index = next(i for i, s in enumerate(all_strikes) if abs(s - short_strike) < 0.01)
        except StopIteration:
          # If exact short strike doesn't exist in long chain (rare), skip
          continue
        # C. Iterate 'k' Ticks DOWN from Short Strike
        # range(1, 3) checks index+1 and index+2
        for k in range(1, max_spread_width + 1):
          target_index = start_index + k
  
          # Stop if we run out of strikes
          if target_index >= len(sorted_long_puts):
            break

          long_put_data = sorted_long_puts[target_index]
          if not long_put_data: continue
          
          # Build position object
          try:
            # Pass the data dictionary to the 'option' argument
            long_put_obj = Quote(**long_put_data)
            new_position = positions.DiagonalPutSpread(short_put_obj, long_put_obj)
            valid_positions.append(new_position)
  
          except (TypeError, KeyError, ValidationError) as e:
            # Added ValidationError to the catch
            print(f"Could not create position {symbol} @ {short_strike}/{short_strike-k}. Error: {e}")
            continue
  return valid_positions
  
def submit_diagonal_spread_order(
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

  payload = build_multileg_payload(tradier_client, underlying_symbol, quantity, trade_dto_list)

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
  tradier_client: TradierAPI,
  underlying_symbol: str, 
  quantity: int,
  trade_dto_list: List # list of nested dicts with {spread meta..., 'short_put', 'long_put'}
)->Dict:
  """
    Builds the API payload for a multileg order from a list of spreads.
    - A list with 1 spread is treated as an 'open' [open] or a 'close' [close].
    - A list with 2 spreads is treated as a 'roll' [open, close].
    """
  legs = []
  # --- Build the common payload keys --- don't change the key names, they are set by API
  payload = {
    'class': 'multileg',
    'symbol': underlying_symbol,
    'duration': 'day'    
  }

  #print(f"trade_dto_list is: {trade_dto_list}")
  position_original_dto = trade_dto_list[0]
  if len(trade_dto_list) == 1:  # this is an open or close    
    # determine if it is a spread open or spread close order
    if position_original_dto['spread_action'] == config.TRADE_ACTION_OPEN:
      legs.append({'symbol': position_original_dto.get('short_put',{}).get('symbol'), 'side': 'sell_to_open'})
      legs.append({'symbol': position_original_dto.get('long_put', {}).get('symbol'), 'side': 'buy_to_open'})
      payload['type']='credit'
    else:
      legs.append({'symbol': position_original_dto.get('short_put',{}).get('symbol'), 'side': 'buy_to_close'})
      legs.append({'symbol': position_original_dto.get('long_put', {}).get('symbol'), 'side': 'sell_to_close'})
      payload['type']='debit'
      
    payload['price'] = f"{position_original_dto.get('net_premium'):.2f}"

    # --- Case 2: Roll a 4-leg position ---
    # TODO:  following line is a hack.  need to deal with 4 legged trade_dto
  elif len(trade_dto_list) == 2:    # this is a roll
    # Convention: The first position is to open, the second is to close.
    position_to_close_dto = trade_dto_list[1]
    short_position_to_close_symbol = position_to_close_dto.get('short_put',{}).get('symbol')
    long_position_to_close_symbol = position_to_close_dto.get('long_put',  {}).get('symbol')
    # Add legs to CLOSE the existing position
    legs.append({'symbol': short_position_to_close_symbol, 'side': 'buy_to_close'})
    legs.append({'symbol': long_position_to_close_symbol, 'side': 'sell_to_close'})
    
    credit_to_open = position_original_dto.get('net_premium')

    #print(f"build multi leg: credit to open: {credit_to_open}")
    # get fresh cost to close
    short_position_to_close_quote = get_quote(tradier_client, short_position_to_close_symbol)
    long_position_to_close_quote =  get_quote(tradier_client, long_position_to_close_symbol)
    cost_to_close = short_position_to_close_quote.get('ask',0) - long_position_to_close_quote.get('bid',0)
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
    
  #print(f"build_multi_leg_payload: payload: {payload}")
  return payload

def get_strike(symbol:str) -> float:
  """
    Extracts the strike price from an OCC option symbol and returns it as a float.
    """
  strike_price = int(symbol[-8:]) / 1000
  return strike_price

def get_expiration_date(symbol: str) -> dt.date | None:
  """
    Extracts the expiration date from an OCC option symbol and returns it as a date object.
    """
  try:
    # The date is always the 6 characters before the last 9 characters (type + strike)
    date_str = symbol[-15:-9]
    # '%y%m%d' tells strptime to parse a 2-digit year, month, and day
    return dt.datetime.strptime(date_str, '%y%m%d').date()
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

def get_net_roll_rom_per_day(pos: positions.DiagonalPutSpread, cost_to_close: float, today: dt.date)-> float:
  dte = (pos.short_put.expiration_date - today).days

  # Check for DTE and valid margin
  if dte <= 0 or not pos.margin or pos.margin <= 0:
    return -float('inf')

  net_roll_credit = pos.net_premium - cost_to_close
  return_on_margin = net_roll_credit / pos.margin 

  return return_on_margin / dte

def find_new_diagonal_trade(t: TradierAPI, 
                            underlying_symbol: str=None,
                            position_to_roll: positions.DiagonalPutSpread=None,
                            max_roll_to_spread: int = server_config.LONG_STRIKE_DELTA_MAX
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

  roll = True if position_to_roll else None

  if roll is None: # use simple open logic
    print("simple open")
    underlying_price = get_underlying_price(t, underlying_symbol)
    short_strike = math.ceil(underlying_price)
    short_expiry = None
  else:     # use roll logic
    short_symbol = position_to_roll.short_put.symbol
    long_symbol = position_to_roll.long_put.symbol
    #print(f"position to roll short symbol: {short_symbol}")
    short_strike = get_strike(short_symbol)

    # fetch raw dictionary quotes
    short_quote = get_quote(t, short_symbol)
    long_quote = get_quote(t, long_symbol)
    
    if not short_quote or not long_quote:
      print(f"Could not fetch live quotes for roll positions: {short_symbol}, {long_symbol}")
      return None
      
    live_position_to_roll = positions.DiagonalPutSpread(short_quote, long_quote)
    cost_to_close = live_position_to_roll.calculate_cost_to_close()

    # 3. Extract the PARSED dates for comparison
    current_short_date = live_position_to_roll.short_put.expiration_date
    current_long_date = live_position_to_roll.long_put.expiration_date

    # Use the parsed date for the search param as well
    short_expiry = current_short_date  

  # get list of valid positions
  print("calling get_valid_diagonal_put_spreads")
  valid_positions = get_valid_diagonal_put_spreads(short_strike=short_strike, 
                                                  tradier_client=t, 
                                                  symbol=underlying_symbol, 
                                                  max_days_out=server_config.MAX_DTE,
                                                  short_expiry=short_expiry,
                                                  max_spread_width = max_roll_to_spread
                                                  )
  number_positions = len(valid_positions)
  print(f"Number of valid positions: {len(valid_positions)}")
  if number_positions == 0:
    print("Halting script - no positions")
    return None

  if roll:
    # positions must have a larger credit to open than the existing spread cost to close    
    valid_positions = [pos for pos in valid_positions if 
                       (pos.net_premium > 0.01 and #abs(cost_to_close) and 
                        pos.short_put.expiration_date >= current_short_date and
                        pos.long_put.expiration_date != current_long_date
                       )
                      ]

    # find best put diag based on highest return on margin per day of trade
    today = dt.date.today()
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
    return None

  print('To Open')
  best_position.print_leg_details()
  best_position.describe()
  # best_position is a position object
  return best_position

def build_leg_dto(spread_dto: Dict, option_index)->Dict:
  """
  NOT USED - needs work.  Delete?
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

def fetch_option_chain_direct(
  tradier_client: "TradierAPI",
  symbol: str,
  expiration: dt.date,
  greeks: bool = False
) -> List[Dict[str, Any]]:
  """
    Fetches an option chain directly from the Tradier API and parses it resiliently.
    
    This bypasses the tradier_python library's default parser, allowing 
    the function to return all *valid* options from a chain, even if 
    some individual options contain bad data (e.g., null bids or asks).
    """
  endpoint = '/v1/markets/options/chains'
  params = {
    'symbol': symbol,
    'expiration': expiration.strftime('%Y-%m-%d'),
    'greeks': greeks
  }

  good_options_data: List[Dict[str, Any]] = []

  try:
    # 1. Make the direct API call.
    # This now correctly assumes .get() returns a dict.
    data = tradier_client.get(endpoint, params=params)

    # 2. Check for API-level errors in the returned dictionary
    if not isinstance(data, dict):
      print(f"Tradier API call for {symbol} {expiration} returned non-dict: {type(data)}")
      return []

      # Check for common error key formats
    if 'errors' in data or 'error' in data:
      api_error = data.get('errors', data.get('error', 'Unknown API error'))
      print(f"Tradier API returned an error for {symbol} {expiration}: {api_error}")
      return []  # Return empty list on API error

      # 3. Safely access the list of options
      # Tradier's structure is {'options': {'option': [...]}}
    options_list = data.get('options', {}).get('option', [])

    # 4. Handle edge case: Tradier returns a dict if only one option exists
    if options_list and not isinstance(options_list, list):
      options_list = [options_list]

    if not options_list:
      # Check if API returned 'null' for the options key
      if data.get('options') == 'null' or data.get('options') is None:
        print(f"No options found for {symbol} on {expiration} (API returned null)")
      else:
        print(f"No options found for {symbol} on {expiration}")
      return []

      # 5. Resiliently parse each option in the list
    for option_data in options_list:
      try:
        # --- This is the key validation block ---
        bid = option_data.get('bid')
        if bid is None or float(bid) <= 0:
          #print(f"Skipping option (bad bid): {option_data.get('description', 'N/A')}")
          continue 

        ask = option_data.get('ask')
        if ask is None or float(ask) <= 0:
          #print(f"Skipping option (bad ask): {option_data.get('description', 'N/A')}")
          continue

        strike = option_data.get('strike')
        if strike is None:
          print(f"Skipping option (no strike): {option_data.get('description', 'N/A')}")
          continue

          # --- If it passes, add it to our list ---

          # We also re-cast the values to ensure they are floats
          # for consistent use downstream.
        option_data['bid'] = float(bid)
        option_data['ask'] = float(ask)
        option_data['strike'] = float(strike)

        good_options_data.append(option_data)

      except (TypeError, ValueError, KeyError) as e:
        # Catches float(None), float("bad_string"), or a missing key
        print(f"Failed to parse one option for {symbol} {expiration}. Error: {e}. Data: {option_data}. Skipping.")
        continue # Skip this bad option

    return good_options_data

  except Exception as e:
    # Catch any other unexpected error (e.g., network, a different .get() failure)
    # This is where your original error was caught.
    print(f"An unexpected error occurred during API call for {symbol} {expiration}: {e}")
    return []  # Return empty list on failure

def get_quote(provider, symbol: str) -> dict:
  """
  Fetches a quote directly from the Tradier API, bypassing the buggy library wrapper.
  Args:
      provider: Can be an environment string (e.g., 'SANDBOX') OR an authenticated TradierAPI client.
      symbol: The symbol to fetch (Equity, Index, or OCC Option)
  Returns:
      dict: The quote data dictionary, or None if not found/error.
  """
  # 1. Resolve the Client
  if isinstance(provider, str):
    tradier_client, _ = get_tradier_client(provider)
  else:
    tradier_client = provider
    
  endpoint = '/v1/markets/quotes'
  params = {'symbols': symbol, 'greeks': 'false'}

  try:
    # 2. Direct API Call
    data = tradier_client.get(endpoint, params=params)

    # 3. Validate Response Structure
    if not isinstance(data, dict):
      print(f"API call for {symbol} returned non-dictionary: {type(data)}")
      return None

    if 'errors' in data or 'error' in data:
      # Use .get() to avoid crashing on structure mismatches
      api_error = data.get('errors', data.get('error', 'Unknown API error'))
      # Only print error if it's NOT just an unmatched symbol (which we handle via fallback)
      if "unmatched" not in str(api_error).lower():
        print(f"Tradier API returned an error for {symbol}: {api_error}")
      return None

    # 4. Parse 'quotes' -> 'quote'
    # Tradier returns {'quote': {...}} for single, or {'quote': [{...}]} for multiple
    quotes_container = data.get('quotes', {})
    quote = None
    if quotes_container:
      quote_data = quotes_container.get('quote')
      # Handle List vs Dict inconsistency
      if isinstance(quote_data, list) and quote_data:
        quote = quote_data[0]
      elif isinstance(quote_data, dict):
        quote = quote_data
      else:
        #print("in get_quote: quote_data: {quote_data} is empty or None, trying weekly 'w' suffix")
        quote = None
    else:
      print("get_quote failed: quotes_container is None")
      return None

    # 5. Index Fallback Logic
    # If we got no data, and it's an index symbol (but not already xxxW), try xxxW
    if not quote:
      for index_root in config.INDEX_SYMBOLS:
        weekly_root = f"{index_root}W"
        if symbol.startswith(index_root) and not symbol.startswith(weekly_root):   #don't keep looping if indexW fails too
          #print(f"lookup failed for {symbol}. Retrying with {symbol}W fallback...")      
          alt_symbol = symbol.replace(index_root, weekly_root, 1)
          # Recursive call with the same client
          return get_quote(tradier_client, alt_symbol)

    if not quote:
      print(f"No quote data returned for {symbol}.") # Optional noise reduction
      return None

    return quote

  except Exception as e:
    print(f"Critical parsing error for {symbol}: {e}")
    return None
'''
def get_quote_direct(tradier_client: "TradierAPI",
                                symbol: str) ->Dict:
  """
    Bypasses the tradier_python model validation to get the quote 
    for both securities and indices.
  """
  
  endpoint = '/v1/markets/quotes'
  
  # Define parameters (note: 'greeks' and 'include_all_roots' must be strings for the API)
  params = {
    'symbols': symbol, 
    'greeks': 'false'
  }

  try:
    # 1. Use the client's session to make the authenticated request
    data = tradier_client.get(endpoint, params=params)
    if not isinstance(data, dict):
      print(f"API call for {symbol} returned non-dictionary: {type(data)}")
      return 0.0
    #print(f"data: {data}")
    if 'errors' in data or 'error' in data:
      api_error = data.get('errors', data.get('error', 'Unknown API error'))
      print(f"Tradier API returned an error for {symbol}: {api_error}")
      return 0.0

    # 3. Safely access the quote (quotes -> quote -> [0])
    # Use .get() with default values to prevent KeyErrors if the structure is missing
    quotes = data.get('quotes', {})
    #print(f"quotes: {quotes}")
    quote = quotes.get('quote', [])
    #print(f"quote: {quote}")
    if not quote:
      print(f"No quote data returned for {symbol}.")
      return 0.0
    return quote

  except requests.exceptions.RequestException as e:
    print(f"Network error fetching quote for {symbol}: {e}")
    return 0.0
  except Exception as e:
    # Catch errors from missing keys or failed float conversion
    print(f"Critical parsing error for {symbol}: {e}")
    return 0.0
'''

def lookup_option_symbol(tradier_client, underlying, expiration, option_type, strike):
  """
  Queries Tradier to find the correct OCC symbol for the given parameters.
  Useful for indices (SPX vs SPXW) where the root symbol varies.
  """
  endpoint = '/v1/markets/options/lookup'

  # 1. Format Parameters
  if isinstance(expiration, dt.date):
    exp_str = expiration.strftime('%Y-%m-%d')
    # Suffix Date Format: YYMMDD
    suffix_date = expiration.strftime('%y%m%d')
  else:
    exp_str = expiration
    # Try to parse string to get YYMMDD, or assume user passed date obj usually
    try:
      d = dt.datetime.strptime(expiration, '%Y-%m-%d')
      suffix_date = d.strftime('%y%m%d')
    except:
      suffix_date = ""

  params = {
    'underlying': underlying,
    'expiration': exp_str,
    'type': option_type.lower(), # API expects 'put' or 'call'
    'strike': str(strike)
  }

  try:
    # 2. Call API
    response = tradier_client.get(endpoint, params=params)
    # Debug print to confirm structure if needed
    # print(f"Lookup response: {response}") 

    # 3. Build Expected Suffix (e.g., 251231P04200000)
    # This allows us to pick the right symbol from the list regardless of the Root
    type_char = 'P' if option_type.lower() == 'put' else 'C'
    strike_int = int(float(strike) * 1000)
    strike_str = f"{strike_int:08d}"
    expected_suffix = f"{suffix_date}{type_char}{strike_str}"

    # 4. Parse Response: {'symbols': [{'rootSymbol': 'SPXW', 'options': [...]}]}
    symbols_data = response.get('symbols', [])

    # Handle case where it might be a single dict instead of list
    if isinstance(symbols_data, dict):
      symbols_data = [symbols_data]

    for root_entry in symbols_data:
      options_list = root_entry.get('options', [])
      for symbol in options_list:
        if symbol.endswith(expected_suffix):
          return symbol

    print(f"No matching symbol found in lookup for suffix {expected_suffix}")
    return None

  except Exception as e:
    print(f"Error looking up option symbol: {e}")
    return None

  except Exception as e:
    print(f"Error looking up option symbol: {e}")
    return None

def fetch_leg_quote(tradier_client, underlying, leg_row):
  """Helper to resolve OCC symbol and fetch quote for any leg."""
  if not leg_row: 
    return None

  if underlying in config.INDEX_SYMBOLS:
    occ = lookup_option_symbol(
      tradier_client, underlying, leg_row['Expiration'], 
      leg_row['OptionType'], leg_row['Strike']
    )
  else:
    occ = build_occ_symbol(
      underlying, leg_row['Expiration'], 
      leg_row['OptionType'], leg_row['Strike']
    )

  if not occ:
    return None

  return get_quote(tradier_client, occ)

def get_underlying_price(tradier_client: TradierAPI, symbol: str) ->float:
  # get underlying price and thus short strike
  underlying_quote = get_quote(tradier_client, symbol)
  #print(f"quote: {underlying_quote}")
  # Extract price: Use 'last' or fallback to 'close' 
  underlying_price = underlying_quote.get('last')
  if underlying_price is None:
    underlying_price = underlying_quote.get('close') 

  if underlying_price is None:
    raise ValueError(f"Price not available in API response for {symbol}")
  #print(f"underlying_price: {underlying_price}")
  return underlying_price

# In server_code/server_helpers.py

def fetch_strikes_direct(tradier_client, symbol, expiration):
  """
  Lightweight fetch of ONLY the strike prices for a given expiration.
  Returns a sorted list of floats: [100.0, 105.0, 110.0]
  """
  endpoint = '/v1/markets/options/strikes'

  if isinstance(expiration, dt.date):
    exp_str = expiration.strftime('%Y-%m-%d')
  else:
    exp_str = expiration

  params = {'symbol': symbol, 'expiration': exp_str}

  try:
    response = tradier_client.get(endpoint, params=params)
    # Structure: {'strikes': {'strike': [100.0, 105.0...]}}

    strikes_container = response.get('strikes', {})
    if not strikes_container:
      return []

    strikes_data = strikes_container.get('strike', [])

    # Handle single item vs list
    if isinstance(strikes_data, (float, int)):
      return [float(strikes_data)]

    # Ensure they are floats and sorted (Tradier usually sorts, but be safe)
    return sorted([float(s) for s in strikes_data])

  except Exception as e:
    print(f"Error fetching strikes for {symbol}: {e}")
    return []

def find_vertical_roll(environment, 
                       underlying_symbol, 
                       current_position: positions.DiagonalPutSpread, 
                       original_credit: float = 0.0,
                       margin_expansion_limit_ticks: int = 0):
  """
  Finds the best 'Roll Out and Down' candidate.
  
  PRIORITY:
  1. TIME: Nearest expiration with Safe Width + "Affordable" Cost.
  2. DEFENSE: Nearest expiration with Safe Width + "Acceptable" Cost.
  3. ESCAPE: If nearest expiry is "Catastrophically" expensive, move to next expiry.
  """
  t, _ = get_tradier_client(environment)

  current_short = current_position.short_put
  current_long = current_position.long_put
  cost_to_close = current_position.calculate_cost_to_close()
  old_width_val = current_short.strike - current_long.strike

  # 1. Define Thresholds
  # Target: Small debit or credit (e.g. keep 90% of profit)
  target_debit_limit = -0.10 * abs(original_credit) 

  # Catastrophic: Do not auto-accept a debit worse than this (e.g. wiping out 80% of profit)
  # If the only options in 12/08 are -$2.00 debits, skip 12/08.
  catastrophic_limit = -0.80 * abs(original_credit) 

  print(f"Scanning Vertical Roll Down for {underlying_symbol}. Cost to Close: ${cost_to_close:.2f}")
  print(f"Target Debit: > ${target_debit_limit:.2f} | Catastrophic Limit: < ${catastrophic_limit:.2f}")

  # 2. Get Candidates (Same search logic as before)
  expirations = get_near_term_expirations(t, underlying_symbol, max_days_out=server_config.MAX_DTE)
  valid_expirations = [e for e in expirations if e > current_short.expiration_date]

  valid_rolls = []
  today = dt.date.today()

  for exp in valid_expirations:
    chain = fetch_option_chain_direct(t, underlying_symbol, exp)
    if not chain: continue

    puts = [o for o in chain if o['option_type'] == 'put']
    puts.sort(key=lambda x: x['strike'], reverse=True)

    short_candidates = [p for p in puts if p['strike'] < current_short.strike]

    for short_opt in short_candidates:
      short_strike = short_opt['strike']
      long_candidates = [p for p in puts if p['strike'] < short_strike]

      for long_opt in long_candidates:
        long_strike = long_opt['strike']
        width = short_strike - long_strike

        if width > old_width_val + 0.01:
          if margin_expansion_limit_ticks == 0: continue
          if width > (old_width_val * 2.5): continue

        try:
          new_credit_to_open = short_opt['bid'] - long_opt['ask']
          net_roll_price = new_credit_to_open - cost_to_close
          new_margin = width * 100
          days_to_exp = max(1, (exp - today).days)
          roll_rroc = (net_roll_price / new_margin) / days_to_exp

          valid_rolls.append({
            'short_leg': short_opt,
            'long_leg': long_opt,
            'expiration': exp,
            'net_roll_price': net_roll_price,
            'new_margin': new_margin,
            'rroc': roll_rroc,
            'width': width
          })
        except: continue

  if not valid_rolls:
    print("No valid roll candidates found.")
    return None

  # --- SELECTION LOGIC ---
  valid_rolls.sort(key=lambda x: x['expiration'])
  best_candidate = None

  for expiration_date, group in groupby(valid_rolls, key=lambda x: x['expiration']):
    group_list = list(group)

    # Filter 1: Safe Width (No Expansion)
    safe_width_candidates = [r for r in group_list if r['width'] <= old_width_val + 0.01]

    if not safe_width_candidates:
      continue

      # Filter 2: Affordable (Better than Target Limit)
    affordable = [r for r in safe_width_candidates if r['net_roll_price'] >= target_debit_limit]

    if affordable:
      # Gold Standard: Near term, Safe Width, Cheap. STOP HERE.
      best_candidate = max(affordable, key=lambda x: x['rroc'])
      print(f"Found TARGET trade in {expiration_date}")
      break

      # Filter 3: Acceptable (Worse than Target, but NOT Catastrophic)
    acceptable = [r for r in safe_width_candidates if r['net_roll_price'] >= catastrophic_limit]

    if acceptable:
      # Silver Standard: Near term, Safe Width, Expensive but survivable. STOP HERE.
      # We prefer to take the hit here rather than adding more time risk.
      # Pick the LEAST BAD price (highest net_roll_price) to minimize cash burn.
      best_candidate = max(acceptable, key=lambda x: x['net_roll_price'])
      print(f"Found ACCEPTABLE trade in {expiration_date}. (Price {best_candidate['net_roll_price']:.2f})")
      break
    else:
      # Bronze Standard: Everything here is Catastrophic.
      # SKIP this expiration. Force the loop to check the next date.
      print(f"Skipping {expiration_date}: Best price is below catastrophic limit.")
      continue

  # 3. Fallback: If we skipped ALL expirations (or found nothing), take Global Best RROC
  if not best_candidate:
    print("No non-catastrophic trades found. Falling back to global best (Expansion allowed).")
    def fallback_score(r):
      score = r['rroc']
      if r['width'] > old_width_val + 0.01: score *= 0.8 
      return score
    best_candidate = max(valid_rolls, key=fallback_score)

  print(f"Best Roll: {best_candidate['short_leg']['strike']}/{best_candidate['long_leg']['strike']} Exp: {best_candidate['expiration']}")
  print(f"Net Price: {best_candidate['net_roll_price']:.2f}")

  try:
    new_short_obj = Quote(**best_candidate['short_leg'])
    new_long_obj = Quote(**best_candidate['long_leg'])
    return positions.DiagonalPutSpread(new_short_obj, new_long_obj)
  except Exception as e:
    print(f"Error building best position object: {e}")
    return None

def get_vertical_spread(t: TradierAPI, 
                        symbol: str = config.DEFAULT_SYMBOL, 
                        target_delta: float = config.DEFAULT_VERTICAL_DELTA, 
                        width: float = None, 
                        quantity: int = None,
                        target_rroc: float = None):
  """
    Finds a 0DTE (or nearest term) Vertical Put Spread based on Delta.
  """
  # 1. Resolve Settings
  settings_row = app_tables.settings.get() or {} # Assumes single-row settings table

  # Defaults
  target_rroc = target_rroc if target_rroc is not None else (settings_row['default_target_rroc'] if settings_row and settings_row['default_target_rroc'] else config.DEFAULT_RROC_HARVEST_TARGET)
  eff_width = width if width is not None else (settings_row['default_width'] if settings_row and settings_row['default_width'] else config.DEFAULT_VERTICAL_WIDTH)
  eff_qty = quantity if quantity is not None else (settings_row['default_qty'] if settings_row and settings_row['default_qty'] else config.DEFAULT_QUANTITY)

  # 2. Find Expiration (Nearest valid date)
  # We re-use your existing helper to find the first valid expiration (Today or Tomorrow)
  expirations = get_near_term_expirations(t, symbol, max_days_out=3)
  if not expirations:
    return {"error": f"No expirations found for {symbol}"}

  target_date = expirations[0] # Pick the nearest one (0DTE logic)

  # 3. Fetch Chain WITH GREEKS
  # We cannot use fetch_option_chain_direct if it hardcodes greeks=False. 
  # Let's make a direct call here to be safe and efficient.
  try:
    endpoint = '/v1/markets/options/chains'
    params = {
      'symbol': symbol, 
      'expiration': target_date.strftime('%Y-%m-%d'), 
      'greeks': 'true' # <--- CRITICAL
    }
    resp = t.get(endpoint, params=params)
    chain = resp.get('options', {}).get('option', [])
    if isinstance(chain, dict): chain = [chain]
  except Exception as e:
    print(f"Error fetching chain: {e}")
    return {"error": "API Error fetching chain."}

    # 4. Find Short Leg (Closest to Delta)
    # Filter for Puts with valid Delta
  puts = [
    p for p in chain 
    if p.get('option_type') == 'put' 
    and p.get('greeks') 
    and p['greeks'].get('delta') is not None
  ]
  if not puts:
    return {"error": "No puts with greeks found."}

    # Find put with delta closest to target (e.g., -0.20)
    # Note: Put delta is negative, so we compare abs(delta)
  def delta_distance(opt):
    d = float(opt['greeks']['delta'])
    return abs(abs(d) - abs(target_delta))

  short_leg = min(puts, key=delta_distance)
  short_strike = float(short_leg['strike'])

  # 5. Find Long Leg (Short Strike - Width)
  target_long_strike = short_strike - eff_width

  # Look for exact match first
  long_leg = next((p for p in puts if abs(float(p['strike']) - target_long_strike) < 0.01), None)

  # Fallback: Find closest strike if exact width doesn't exist
  if not long_leg:
    def strike_distance(opt):
      return abs(float(opt['strike']) - target_long_strike)
      # Filter to only strikes BELOW short strike
    lower_puts = [p for p in puts if float(p['strike']) < short_strike]
    if not lower_puts:
      return {"error": "No valid long legs found below short strike."}
    long_leg = min(lower_puts, key=strike_distance)

  actual_width = short_strike - float(long_leg['strike'])
  #print(f"short: {short_leg}, long: {long_leg}")
  # 6. Financial Calculations
  short_bid = float(short_leg.get('bid', 0) or 0) # Handle None safely
  long_ask = float(long_leg.get('ask', 0) or 0)
  gross_credit = short_bid - long_ask
  #print(f"short_bid: {short_bid}, long_ask: {long_ask}, gross_credit: {gross_credit}")
  margin_per_spread = (actual_width * 100) - (gross_credit * 100) # Assuming standard x100
  # Actually, your reference used logic: margin = width - credit. 
  # Usually margin on vertical is (Width * Multiplier) - Credit. 
  # Let's stick to your simple unit math if that's what you prefer, 
  # OR use standard $ calculations:

  # Standard:
  credit_total = gross_credit * eff_qty * 100
  margin_total = (actual_width * eff_qty * 100) - credit_total

  # Harvest: 5% of risk? Or your specific formula?
  # Your formula: harvest_target_per_contract = margin_per_spread * 0.05
  # Let's keep your formula structure but ensure floats are safe

  simple_margin = actual_width - gross_credit
  #print(f"margin_per_spread: {margin_per_spread}, credit_total: {credit_total}, margin_total: {margin_total}, simple_margin: {simple_margin}")
  harvest_amt = simple_margin * target_rroc
  #print(f"harvest_target: {target_rroc}, harvest_amt: {harvest_amt}")
  return_dict = {
    "status": "success",
    "parameters": {
      "symbol": symbol,
      "expiration": target_date.strftime('%Y-%m-%d'),
      "width": actual_width,
      "quantity": eff_qty,
      "target_delta": target_delta
    },
    "legs": {
      "short": {
        "symbol": short_leg['symbol'],
        "strike": short_strike,
        "delta": short_leg['greeks']['delta'],
        "bid": short_bid,
        "description": short_leg.get('description', '')
      },
      "long": {
        "symbol": long_leg['symbol'],
        "strike": long_leg['strike'],
        "delta": long_leg['greeks']['delta'],
        "ask": long_ask,
        "description": long_leg.get('description', '')
      }
    },
    "financials": {
      "credit_per_contract": round(gross_credit, 2),
      "margin_per_contract": round(margin_per_spread, 2), # <--- Add this line
      "total_credit": round(credit_total, 2),
      "total_margin_risk": round(margin_total, 2),
      "harvest_price": round(gross_credit - harvest_amt, 2)
    }
  }
  #print(f"return_dict: {return_dict}")
  return return_dict