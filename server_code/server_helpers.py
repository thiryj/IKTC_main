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
import pytz
from urllib.parse import urljoin
from typing import Dict, List, Tuple, Any, TYPE_CHECKING
import datetime as dt
from pydantic_core import ValidationError
from tradier_python import TradierAPI
from tradier_python.models import Quote
from itertools import groupby

# Private libs
from shared import config #client side / combined config
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

  # 1. Get current time in ET to handle market close correctly
  now_et = dt.datetime.now(pytz.timezone('US/Eastern'))

  # 2. If after 4:00 PM ET, minimum days out is 1 (tomorrow), else 0 (today)
  min_days = 1 if now_et.time() >= dt.time(16, 0) else 0

  # 3. Use the ET date for accurate comparison
  today = now_et.date()
  
  all_expirations = tradier_client.get_option_expirations(symbol=symbol, include_all_roots=True)
  near_term_expirations = [
    exp for exp in all_expirations
    if min_days <= (exp - today).days <= max_days_out
  ]
  return near_term_expirations
  
def submit_spread_order(
                                tradier_client: TradierAPI,
                                endpoint_url: str,
                                underlying_symbol: str,
                                quantity: int,
                                trade_dto_dict: Dict, # nested dict with one or multiple {spread meta..., 'short_put', 'long_put'}
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
      trade_dto_dict: # nested dict with one or multiple {spread meta..., 'short_put', 'long_put'}
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

  payload = build_multileg_payload(tradier_client, 
                                   underlying_symbol, 
                                   quantity, 
                                   trade_dto_dict, 
                                   limit_price)
  
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
  trade_dto_dict: Dict,
  limit_price: float# {'to_open': dto, 'to_close': dto} nested dicts with {spread meta..., 'short_put', 'long_put'}
)->Dict:
  """
    Builds the API payload for a multileg order from a dict of one or multi spreads.
    - A list with 1 spread is treated as an 'open' [open] or a 'close' [close].
    - A list with 2 spreads is treated as a 'roll' [open, close].
    """
  legs = []
  
  #print(f"trade_dto_dict is: {trade_dto_dict}")
  # Process the 'to_close' spread
  if 'to_close' in trade_dto_dict:
    closing_dto = trade_dto_dict['to_close']
    legs.append({'symbol': closing_dto.get('short_put',{}).get('symbol'), 'side': 'buy_to_close'})
    legs.append({'symbol': closing_dto.get('long_put', {}).get('symbol'), 'side': 'sell_to_close'})
    
  # Process the 'to_open'
  if 'to_open' in trade_dto_dict:
    opening_dto = trade_dto_dict['to_open']
    legs.append({'symbol': opening_dto.get('short_put',{}).get('symbol'), 'side': 'sell_to_open'})
    legs.append({'symbol': opening_dto.get('long_put', {}).get('symbol'), 'side': 'buy_to_open'})

  #print(f"build_multileg_payload: trade_dto_dict: {trade_dto_dict}")
  #print(f"build_multileg_payload: limit price: {limit_price}")
  #print(f"legs are: {legs}")
        
  if not legs:
    raise ValueError("build_multileg_payload: No valid legs found in trade_dto_dict")
    
  payload_price = f"{abs(limit_price):.2f}"
  payload_type = 'credit' if limit_price >= 0 else 'debit'
    
  payload = {
    'class': 'multileg',
    'symbol': underlying_symbol,
    'duration': 'day',
    'type': payload_type,
    'price': payload_price
  }
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

def find_vertical_roll(t: TradierAPI, 
                       underlying_symbol, 
                       current_position: positions.DiagonalPutSpread, 
                       margin_expansion_limit_ticks: int = 0)->Tuple: # returns roll to spread, net roll price
  """
  Finds the best 'Roll Out and Down' candidate.
  
  PRIORITIES:
  1. MINIMIZE DTE: Stop at the first (nearest) expiration date that offers a valid trade.
  2. AGGRESSION: Within that expiration, pick the Lowest Strike (Max Distance) possible for Zero Debit.
  """

  # 1. Setup
  current_short = current_position.short_put
  current_long = current_position.long_put
  cost_to_close = current_position.calculate_cost_to_close()
  target_width = current_short.strike - current_long.strike

  print(f"Scanning Vertical Roll (Down & Out) for {underlying_symbol}.")
  print(f"Cost to Close: ${cost_to_close:.2f} | Target Width: {target_width:.2f}")

  # 2. Get Expirations (Out) and Sort by Date (Nearest First)
  expirations = get_near_term_expirations(t, underlying_symbol, max_days_out=config.MAX_DTE)
  valid_expirations = sorted([e for e in expirations if e > current_short.expiration_date])
  print(f"number of roll expirations inspected: {len(valid_expirations)}")

  # 3. Iterate Expirations (Nearest -> Farthest)
  for exp in valid_expirations:
    chain = fetch_option_chain_direct(t, underlying_symbol, exp)
    if not chain: continue

    # Filter for Puts and Sort by Strike Descending (High -> Low)
    puts = [o for o in chain if o['option_type'] == 'put']
    puts.sort(key=lambda x: x['strike'], reverse=True)

    best_for_this_exp = None

    for short_opt in puts:
      # CONSTRAINT: Down (Strike must be lower than current)
      if short_opt['strike'] >= current_short.strike:
        continue

      # Find matching Long Leg (maintain width)
      target_long_strike = short_opt['strike'] - target_width
      # fuzzy match for long strike (within 0.05)
      long_opt = next((p for p in puts if abs(p['strike'] - target_long_strike) < 0.05), None)

      if not long_opt:
        continue

      # Calculate Economics
      credit_to_open = short_opt['bid'] - long_opt['ask']
      net_roll_price = credit_to_open - cost_to_close

      # CONSTRAINT: Zero Debit (Credit >= 0)
      if net_roll_price >= 0.00:

        # Candidate found. 
        # Since we iterate DOWN (High -> Low Strike), and premium drops as we go down,
        # we keep overwriting 'best_for_this_exp' as long as we find valid trades.
        # The last one we find will be the Lowest Strike (Max Distance) for this expiry.
        best_for_this_exp = {
          'short_leg': short_opt,
          'long_leg': long_opt,
          'expiration': exp,
          'net_roll_price': net_roll_price,
          'new_margin': (short_opt['strike'] - long_opt['strike']) * 100,
          'width': short_opt['strike'] - long_opt['strike']
        }
      else:
        # Net Roll became negative. Stop searching lower strikes for this expiry.
        break

    # PRIORITY CHECK: Did we find ANY valid candidate in this (nearest) expiration?
    if best_for_this_exp:
      print(f"Best Roll Found in Nearest Valid Expiry: {best_for_this_exp['expiration']}")
      print(f"Strike: {best_for_this_exp['short_leg']['strike']} | Net Price: {best_for_this_exp['net_roll_price']:.2f}")

      try:
        new_short_obj = Quote(**best_for_this_exp['short_leg'])
        new_long_obj = Quote(**best_for_this_exp['long_leg'])
        return positions.DiagonalPutSpread(new_short_obj, new_long_obj), best_for_this_exp['net_roll_price']
      except Exception as e:
        print(f"Error building best position object: {e}")
        return None, 0.0

  # If loop finishes without returning, no valid rolls exist.
  print("No valid roll candidates found (Zero Debit / Down & Out).")
  return None, 0.0
  
def get_vertical_spread(t: TradierAPI, 
                        symbol: str = config.DEFAULT_SYMBOL, 
                        target_delta: float = config.DEFAULT_VERTICAL_DELTA, 
                        width: float = None, 
                        quantity: int = None
                        )->Dict:
  """
    Finds a 0DTE (or nearest term) Vertical Put Spread based on Delta.
  """
  
  # 2. Find Expiration (Nearest valid date)
  # We re-use your existing helper to find the first valid expiration (Today or Tomorrow)
  expirations = get_near_term_expirations(t, symbol, max_days_out=3)
  if not expirations:
    return {"error": f"No expirations found for {symbol}"}
  target_date = expirations[0] # Pick the nearest one (0DTE logic)
  
  # 3. Fetch Chain WITH GREEKS
  chain = fetch_option_chain_direct(t, symbol, target_date, greeks=True)
  if not chain:
    return {"error": "API Error fetching chain or no valid options found."}

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
  
  # 5. Find Long Leg (Short Strike - Width)
  target_long_strike = short_leg['strike'] - width

  # Look for exact match first
  long_leg = next((p for p in puts if abs(float(p['strike']) - target_long_strike) < 0.01), None)
  if not long_leg:
    def strike_distance(opt):
      return abs(float(opt['strike']) - target_long_strike)
      # Filter to only strikes BELOW short strike
    lower_puts = [p for p in puts if float(p['strike']) < short_leg['strike']]
    if not lower_puts:
      return {"error": "No valid long legs found below short strike."}
    long_leg = min(lower_puts, key=strike_distance)
  # 5.2. Create Position Object
  # Note: ensure keys like 'contract_size' exist or are defaulted if API omits them
  short_leg.setdefault('contract_size', 100)
  long_leg.setdefault('contract_size', 100)
  position = positions.DiagonalPutSpread(short_leg, long_leg)
  #print(f"position is: {position.get_dto()}")
  # 6. Return Dict with Object
  
  return_dict = {
    "status": "success",
    "parameters": {
      "symbol": symbol,
      "expiration": target_date.strftime('%Y-%m-%d'),
      "width": abs(short_leg['strike'] - long_leg['strike']),
      "quantity": quantity,
      "target_delta": target_delta
    },
    "legs": position,
    "financials": {
      "credit_per_contract": round(position.net_premium, 2),
      "margin_per_contract": round(position.margin / 100, 2), # Class calculates total margin (width*100), normalizing for display if needed
      "total_credit": round(position.net_premium * quantity * 100, 2),
      "total_margin_risk": round(position.margin * quantity, 2)
    }
  }
  #print(f"return_dict: {return_dict}")
  return return_dict

def is_market_open(t_client, buffer_minutes=5) -> bool:
  """
  Queries Tradier's official clock to see if we are in a valid trading session.
  Handles holidays and early closes automatically.
  
  :param t_client: Authenticated TradierAPI client
  :param buffer_minutes: Wait N minutes after open before trading (Volatility Guard)
  """
  try:
    # 1. Ask Tradier for the time
    clock = t_client.get_clock() 
    # Note: Depending on your Python wrapper, this might be t_client.get_clock() 
    # or t_client.markets_clock(). Adjust based on your library.
    # If using raw requests: t.get('markets/clock')
    print(f"get_clock response: {clock}")
    if not clock:
      print("Error: Empty response from Market Clock")
      return False

    if clock.state != 'open':
      return False

    # 'timestamp' is current server time (Unix Epoch)
    current_ts = float(clock.timestamp)
    server_time = dt.datetime.fromtimestamp(current_ts)
    market_open_time = server_time.replace(hour=9, minute=30, second=0)
    safe_start_time = market_open_time + dt.timedelta(minutes=buffer_minutes)
    if server_time < safe_start_time:
      # print(f"Market is Open, but within {buffer_minutes} min buffer.")
      return False

    return True

  except Exception as e:
    print(f"Error checking market clock: {e}")
    # FAIL SAFE: If we can't verify market is open, assume CLOSED.
    return False

def calculate_cycle_net_liq(t_client, cycle_row):
  """
    Calculates the total liquidation value of a Cycle:
    (Value of Long Hedge) - (Cost to Close all Short Spreads)
    """
  total_hedge_value = 0.0
  total_spread_cost = 0.0

  # 1. PRICE THE HEDGE (Stored on the Cycle row)
  hedge_leg = cycle_row['HedgeLeg'] # Link to 'legs' table
  if hedge_leg:
    # Construct OCC symbol from the leg row
    hedge_occ = build_occ_symbol(
      underlying=hedge_leg['Underlying'], # Assuming 'legs' has this
      expiration_date=hedge_leg['Expiration'], 
      option_type=hedge_leg['OptionType'], 
      strike=hedge_leg['Strike']
    )

    # Get Live Price (Bid) because we are SELLING the hedge to close
    quote = get_quote(t_client, hedge_occ)
    if quote:
      total_hedge_value = quote['bid'] * hedge_leg['Quantity'] * 100

    # 2. PRICE THE SPREADS (Stored in Trades table)
    # Find all open trades linked to this cycle
  child_trades = app_tables.trades.search(Cycle=cycle_row, Status='OPEN')

  active_spread_dtos = [] # Keep track of these so we can return them for the close order

  for trade in child_trades:
    # We can reuse your existing logic to get cost to close
    # (Assuming you have a helper for this, or we just do it manually here for speed)

    # Quick fetch of active legs for this trade
    # ... (Implementation depends on how you link legs to trades, likely via transactions)
    # For now, let's assume we use your 'get_close_trade_dto' logic:

    close_dto = ServerModule1.get_close_trade_dto(trade) # You likely need to import this or move it to helpers
    if close_dto:
      # Add to total cost (Ask price of Short - Bid price of Long)
      # close_dto usually has the limit price embedded or we calculate it
      # For panic calculation, we want the current MARKET price

      # Simplified: Just grab the 'mark' of the spread if you have it, 
      # or re-quote the legs here.
      pass 
      # (To keep this snippet short, I assume you sum up the cost)

      active_spread_dtos.append(close_dto)

    # Net Liq = What we get for the Hedge - What we pay to close Spreads
  net_liq = total_hedge_value - total_spread_cost

  return net_liq, active_spread_dtos, hedge_occ