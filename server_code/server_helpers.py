import anvil.secrets
import anvil.server
import datetime as dt
import pytz
import math
from typing import Dict, List, Tuple, Any
from urllib.parse import urljoin

# External Libs
import requests
from tradier_python import TradierAPI
from tradier_python.models import Quote

# Internal Libs
from shared import config 
import positions

# ------------------------------------------------------------------
#  INFRASTRUCTURE & API CLIENTS
# ------------------------------------------------------------------

def get_tradier_client(environment: str) -> Tuple[TradierAPI, str]:
  """Gets an authenticated Tradier client based on environment."""
  env_prefix = environment.upper()
  api_key = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_API_KEY')
  account_id = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_ACCOUNT')
  endpoint_url = anvil.secrets.get_secret(f'{env_prefix}_ENDPOINT_URL')

  t = TradierAPI(token=api_key, default_account_id=account_id, endpoint=endpoint_url)
  return t, endpoint_url

def get_quote(provider, symbol: str) -> dict:
  """Reliable fetch of a single quote dict."""
  if isinstance(provider, str):
    tradier_client, _ = get_tradier_client(provider)
  else:
    tradier_client = provider

  try:
    data = tradier_client.get('/v1/markets/quotes', params={'symbols': symbol, 'greeks': 'false'})
    quote = data.get('quotes', {}).get('quote')
    print(f"in s.h.get_quote, quote: {quote}")
    # Handle list vs dict response
    if isinstance(quote, list) and quote:
      return quote[0]
    return quote if isinstance(quote, dict) else None
  except Exception as e:
    print(f"Error fetching quote for {symbol}: {e}")
    return None

def fetch_option_chain_direct(tradier_client: TradierAPI, symbol: str, expiration: dt.date) -> List[Dict]:
  """
  Robust fetch of option chain, filtering out bad data.
  """
  params = {
    'symbol': symbol,
    'expiration': expiration.strftime('%Y-%m-%d'),
    'greeks': 'true'
  }

  try:
    data = tradier_client.get('/v1/markets/options/chains', params=params)
    options_list = data.get('options', {}).get('option', [])

    if isinstance(options_list, dict): 
      options_list = [options_list]

    valid_options = []
    for opt in options_list:
      # Filter bad data
      if not opt.get('strike') or not opt.get('bid') or not opt.get('ask'):
        continue

      # Cast to correct types
      opt['strike'] = float(opt['strike'])
      opt['bid'] = float(opt['bid'])
      opt['ask'] = float(opt['ask'])
      valid_options.append(opt)

    return valid_options
  except Exception as e:
    print(f"Error fetching chain: {e}")
    return []

def get_near_term_expirations(tradier_client: TradierAPI, symbol: str, max_days_out: int = 45) -> List[dt.date]:
  """Returns sorted list of valid expiration dates."""
  try:
    now_et = dt.datetime.now(pytz.timezone('US/Eastern'))
    today = now_et.date()
    # If after 4pm ET, don't include today
    min_days = 1 if now_et.time() >= dt.time(16, 0) else 0

    all_exps = tradier_client.get_option_expirations(symbol=symbol, include_all_roots=True)
    return sorted([e for e in all_exps if min_days <= (e - today).days <= max_days_out])
  except Exception:
    return []

# ------------------------------------------------------------------
#  CORE LOGIC: VERTICAL SPREAD (PutSpread)
# ------------------------------------------------------------------

def find_vertical_roll(t: TradierAPI, 
                       underlying_symbol: str, 
                       current_position: positions.PutSpread) -> positions.PutSpread:
  """
  Finds the best 'Roll Out and Down' candidate.
  """

  # 1. Setup
  current_short = current_position.short_put
  cost_to_close = current_position.calculate_cost_to_close()
  # We enforce the default width from config for the NEW spread, 
  # regardless of old width, to standardize the cycle.
  target_width = config.DEFAULT_WIDTH 

  print(f"Scanning Roll for {underlying_symbol}. Cost to Close: ${cost_to_close:.2f}")

  # 2. Get Expirations (Out)
  # Look out up to 90 days
  expirations = get_near_term_expirations(t, underlying_symbol, max_days_out=90)
  valid_expirations = [e for e in expirations if e > current_short.expiration_date]

  if not valid_expirations:
    print("No valid future expirations found.")
    return None

  # 3. Iterate Expirations (Nearest -> Farthest)
  for exp in valid_expirations:
    chain = fetch_option_chain_direct(t, underlying_symbol, exp)
    if not chain: continue

    # Filter for Puts
    puts = [o for o in chain if o['option_type'] == config.OPTION_TYPE_PUT.lower()]
    # Sort High Strike -> Low Strike
    puts.sort(key=lambda x: x['strike'], reverse=True)

    best_for_this_exp = None

    # Scan Strikes Downward
    for short_opt in puts:
      # CONSTRAINT: Down (Strike must be lower than current)
      if short_opt['strike'] >= current_short.strike:
        continue

      # Find matching Long Leg
      target_long_strike = short_opt['strike'] - target_width
      # Fuzzy match long leg (within 0.05)
      long_opt = next((p for p in puts if abs(p['strike'] - target_long_strike) < 0.05), None)

      if not long_opt:
        continue

      # Calculate Economics
      credit_to_open = short_opt['bid'] - long_opt['ask']
      net_roll_price = credit_to_open - cost_to_close

      # CONSTRAINT: Zero Debit (Credit >= Cost)
      # using -0.01 tolerance to handle floating point noise
      if net_roll_price >= -0.01:
        # We found a valid candidate.
        best_for_this_exp = {
          'short_leg': short_opt,
          'long_leg': long_opt,
          'expiration': exp,
          'net_roll_price': net_roll_price
        }
      else:
        # If we hit a negative roll price, going lower will only get worse.
        break

    # PRIORITY: If we found ANY candidate in this Nearest Expiration, take it.
    if best_for_this_exp:
      print(f"Target found in {best_for_this_exp['expiration']}. "
            f"Strikes: {best_for_this_exp['short_leg']['strike']} / {best_for_this_exp['long_leg']['strike']}. "
            f"Net: {best_for_this_exp['net_roll_price']:.2f}")

      try:
        # Convert dicts back to Quote objects for the wrapper
        new_short_obj = Quote(**best_for_this_exp['short_leg'])
        new_long_obj = Quote(**best_for_this_exp['long_leg'])
        return positions.PutSpread(new_short_obj, new_long_obj)
      except Exception as e:
        print(f"Error building position object: {e}")
        return None

  print("No valid zero-debit roll found in any term.")
  return None

def get_vertical_spread(t: TradierAPI, 
                        symbol: str, 
                        target_dte: int = 45) -> positions.PutSpread:
  """
  Finds a new Vertical Put Spread based on config defaults.
  """

  # 1. Find Expiration
  expirations = get_near_term_expirations(t, symbol, max_days_out=90)
  if not expirations:
    return None

  # Find date closest to target_dte
  target_date_obj = dt.date.today() + dt.timedelta(days=target_dte)
  # min() logic: finds exp with smallest absolute difference in days
  best_exp = min(expirations, key=lambda d: abs((d - target_date_obj).days))

  # 2. Get Chain
  chain = fetch_option_chain_direct(t, symbol, best_exp)
  if not chain:
    return None

  puts = [o for o in chain if o['option_type'] == config.OPTION_TYPE_PUT.lower()]

  # 3. Find Short Leg (Closest to Delta)
  target_delta = abs(config.DEFAULT_VERTICAL_DELTA)

  # Filter for puts that actually have delta data
  puts_with_delta = [p for p in puts if p.get('greeks') and p['greeks'].get('delta')]

  if not puts_with_delta:
    return None

  # Sort by distance to target delta
  puts_with_delta.sort(key=lambda x: abs(abs(float(x['greeks']['delta'])) - target_delta))

  # 4. Find valid spread
  for short_leg in puts_with_delta:
    target_long_strike = short_leg['strike'] - config.DEFAULT_WIDTH

    long_leg = next((p for p in puts if abs(p['strike'] - target_long_strike) < 0.05), None)

    if long_leg:
      return positions.PutSpread(Quote(**short_leg), Quote(**long_leg))

  return None

# ------------------------------------------------------------------
#  ORDER SUBMISSION (Preserved)
# ------------------------------------------------------------------

def build_multileg_payload(tradier_client, underlying_symbol, quantity, trade_dto_list):
  """Builds 4-leg roll or 2-leg open/close payloads."""
  legs = []
  payload = {
    'class': 'multileg',
    'symbol': underlying_symbol,
    'duration': 'day'    
  }

  position_original_dto = trade_dto_list[0]

  # --- CASE 1: Open or Close (2 Legs) ---
  if len(trade_dto_list) == 1:
    action = position_original_dto.get('spread_action')

    if action == config.TRADE_ACTION_OPEN:
      legs.append({'symbol': position_original_dto['short_put']['symbol'], 'side': 'sell_to_open'})
      legs.append({'symbol': position_original_dto['long_put']['symbol'], 'side': 'buy_to_open'})
      payload['type'] = 'credit'
      payload['price'] = f"{position_original_dto['net_premium']:.2f}"
    else: # Close
      legs.append({'symbol': position_original_dto['short_put']['symbol'], 'side': 'buy_to_close'})
      legs.append({'symbol': position_original_dto['long_put']['symbol'], 'side': 'sell_to_close'})
      payload['type'] = 'debit'
      # Cost to close is Debit
      payload['price'] = f"{position_original_dto['cost_to_close']:.2f}"

  # --- CASE 2: Roll (4 Legs) ---
  elif len(trade_dto_list) == 2:
    open_dto = trade_dto_list[0]
    close_dto = trade_dto_list[1]
    
    legs.append({'symbol': open_dto['short_put']['symbol'], 'side': 'sell_to_open'})
    legs.append({'symbol': open_dto['long_put']['symbol'], 'side': 'buy_to_open'})
    legs.append({'symbol': close_dto['short_put']['symbol'], 'side': 'buy_to_close'})
    legs.append({'symbol': close_dto['long_put']['symbol'], 'side': 'sell_to_close'})
    
    net_price = open_dto['net_premium'] - close_dto['cost_to_close']
    payload['type'] = 'credit' if net_price >= 0 else 'debit'
    payload['price'] = f"{abs(net_price):.2f}"

  else:
    print("Error: trade_list must contain 1 or 2 positions.")
    return None

  for i, leg in enumerate(legs):
    payload[f'option_symbol[{i}]'] = leg['symbol']
    payload[f'side[{i}]'] = leg['side']
    payload[f'quantity[{i}]'] = quantity
    
  return payload

def submit_spread_order(tradier_client, 
                        endpoint_url, 
                        underlying_symbol, 
                        quantity, 
                        trade_dto_list, 
                        preview=True, 
                        limit_price=None):
  path = f"accounts/{tradier_client.default_account_id}/orders"
  api_url = urljoin(endpoint_url, path)

  payload = build_multileg_payload(tradier_client, underlying_symbol, quantity, trade_dto_list)
  if not payload: return {'error': 'Failed to build payload'}
  print(f"payload: {payload}")

  if limit_price is not None:
    payload['price'] = f"{float(limit_price):.2f}"

  if preview:
    payload['preview'] = 'true'

  try:
    response = tradier_client.session.post(api_url, data=payload, headers={'accept': 'application/json'})
    response.raise_for_status()
    return response.json()
  except requests.exceptions.HTTPError as e:
    print(f"API Error: {e.response.text}")
    return {'order': {'status': 'error', 'errors': {'error': [e.response.text]}}}
  except Exception as e:
    print(f"Submission Error: {e}")
    return {'order': {'status': 'error', 'errors': {'error': [str(e)]}}}

# ------------------------------------------------------------------
#  HELPER UTILS
# ------------------------------------------------------------------

def build_occ_symbol(underlying, expiration_date, option_type, strike, root_override=None):
  """Builds OCC symbol string."""
  if isinstance(expiration_date, str):
      expiration_date = dt.datetime.strptime(expiration_date, "%Y-%m-%d").date()
      
  # Use override if provided (e.g. 'SPXW'), otherwise default to underlying
  root = root_override if root_override else underlying
  
  exp_str = expiration_date.strftime('%y%m%d')
  type_char = 'P' if str(option_type).upper() == 'PUT' else 'C'
  strike_int = int(float(strike) * 1000)
  symbol = f"{root}{exp_str}{type_char}{strike_int:08d}"
  print(f"build_occ_symbol: {symbol}")
  return symbol

def get_underlying_price(tradier_client, symbol):
  q = get_quote(tradier_client, symbol)
  return q.get('last') if q else 0.0

def fetch_leg_quote(tradier_client, underlying, leg_row)->Dict:
  """
  Helper to fetch quote for a DB leg row.
  Handles the SPX vs SPXW fallback logic.
  """
  if not leg_row:
    return None

  # 2. Build Symbol (Try Standard first)
  occ = build_occ_symbol(underlying, leg_row['Expiration'], leg_row['OptionType'], leg_row['Strike'])
  q = get_quote(tradier_client, occ)
  
  # 3. Fallback: If SPX standard failed, try SPXW
  if not q and underlying == 'SPX':
    occ_weekly = build_occ_symbol(underlying, leg_row['Expiration'], leg_row['OptionType'], leg_row['Strike'], root_override='SPXW')
    q = get_quote(tradier_client, occ_weekly)
    
  return q