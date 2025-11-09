import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server
import requests
from urllib.parse import urljoin
from typing import Dict, List, Tuple
from datetime import date, datetime
from pydantic_core import ValidationError
from tradier_python import TradierAPI
import server_config
import positions

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
                                   max_days_out: int = 10)->List[positions.DiagonalPutSpread]:
  # get list of valid expirations
  expirations = get_near_term_expirations(tradier_client=tradier_client, symbol=symbol, max_days_out=max_days_out)
  exp_count = len(expirations)
  valid_positions = []
  for i in range(3):
    short_put_expiration = expirations[i]
    for j in range(i+1, exp_count):
      long_put_expiration = expirations[j]

      # grab the chains for this valid pair of short put + long put
      try:
        short_put_chain = tradier_client.get_option_chains(symbol=symbol, expiration=short_put_expiration.strftime('%Y-%m-%d'), greeks=False)
      except ValidationError as e:
        continue
      try:
        long_put_chain = tradier_client.get_option_chains(symbol=symbol, expiration=long_put_expiration.strftime('%Y-%m-%d'), greeks=False)
      except ValidationError as e:
        continue

        # for a valid expiration pair, iterate through long put strikes
      for k in range(1, server_config.LONG_STRIKE_DELTA_MAX):

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
  tradier_client: TradierAPI,
  endpoint_url: str,
  underlying_symbol: str,
  quantity: int,
  trade_dto: Dict,
  preview: bool = True,
  limit_price: float = None,
  trade_type: str = None
) -> Dict:
  """
  Submits a multi-leg option order directly using the session,
  bypassing the buggy library helper functions. Can be used for previews.

  Args:
      tradier_client (TradierAPI): The initialized API client.
      endpoint_url (str): The endpoint url
      underlying_symbol (str)
      quantity (int): The number of contracts for each leg.
      trade_dto: The list of positions.  First is to open, second (optional) is to close
      preview (bool, optional): If True, submits as a preview order.
                                Defaults to False.

  Returns:
      Dict: The JSON response from the API as a dictionary, or None if an
            error occurred.
  """
  #api_url = f"{endpoint_url}/accounts/{tradier_client.default_account_id}/orders"
  path = f"/accounts/{tradier_client.default_account_id}/orders"
  api_url = urljoin(endpoint_url, path)

  payload = build_multileg_payload(underlying_symbol, quantity, trade_dto)

  # override price if limit_price sent in
  if limit_price is not None:
    payload['price'] = f"{limit_price:.2f}"

  # Conditionally add the 'preview' or 'type' parameter based on the flag
  if preview:
    payload['preview'] = 'true'

  try:
    response = tradier_client.session.post(api_url, data=payload)
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
  underlying_symbol: str, 
  quantity: int, 
  trade_dto: Dict,
  trade_type: str=None
):
  """
    Builds the API payload for a multileg order from a list of positions.
    - A list with 1 position is treated as an 'open'.
    - A list with 2 positions is treated as a 'roll' [open, close].
    """
  legs = []
  # --- Build the common payload keys ---
  payload = {
    'class': 'multileg',
    'symbol': underlying_symbol,
    'duration': 'day',
    'type': 'credit',
  }

  #position_to_open = trade_dto[]
  short_leg_symbol = trade_dto['short_put']['symbol']
  long_leg_symbol = trade_dto['long_put']['symbol']
  legs.append({'symbol': short_leg_symbol, 'side': 'sell_to_open'})
  legs.append({'symbol': long_leg_symbol, 'side': 'buy_to_open'})
  if trade_type is None or trade_type == 'open':
    payload['price'] = f"{trade_dto['net_premium']:.2f}"
    payload['type'] = 'credit'

    # --- Case 2: Roll a 4-leg position ---
  elif trade_type == 'roll':
    """
    # Convention: The first position is to open, the second is to close.
    position_to_close = trade_list[1]

    # Add legs to CLOSE the existing position
    legs.append({'symbol': position_to_close.short_put.symbol, 'side': 'buy_to_close'})
    legs.append({'symbol': position_to_close.long_put.symbol, 'side': 'sell_to_close'})
    credit_to_open = position_to_open.net_premium
    cost_to_close = position_to_close.calculate_cost_to_close()
    roll_value = credit_to_open - cost_to_close

    payload['price'] = f"{abs(roll_value):.2f}"
    payload['type'] = 'credit' if roll_value >= 0 else 'debit'
    """
    pass

  else:
    # Handle invalid input
    print("Error: trade_list must contain 1 or 2 positions.")
    return None

    # Dynamically add each leg and its quantity to the payload
  for i, leg in enumerate(legs):
    payload[f'option_symbol[{i}]'] = leg['symbol']
    payload[f'side[{i}]'] = leg['side']
    payload[f'quantity[{i}]'] = quantity # Assumes same quantity for all new/closing legs

  return payload

def get_strike(symbol:str) -> float:
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
  type_char = 'P' if option_type == 'Put' else 'C'

  # Format strike to 8-digit string, (e.g., 247 -> '00247000')
  # This assumes strike is a number. We multiply by 1000 and pad with zeros.
  strike_int = int(strike * 1000)
  strike_str = f"{strike_int:08d}"

  # Combine all parts
  return f"{underlying}{exp_str}{type_char}{strike_str}"
