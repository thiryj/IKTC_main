import anvil.secrets
import anvil.server
import datetime as dt
import requests
from urllib.parse import urljoin
from typing import Dict, List, Any, Optional

from tradier_python import TradierAPI
from shared import config

IS_PROD = False   # TODO: get this from settings or UI or console arg
CURRENT_ENV = config.ENV_PROD if IS_PROD else config.ENV_SANDBOX

# Global cache variable (starts empty)
_CACHED_CLIENT = None

# --- AUTHENTICATION ---
def _get_client() -> TradierAPI:
  """Returns the cached client if exist or a new authenticated TradierAPI client based on the current environment"""  
  global _CACHED_CLIENT
  if _CACHED_CLIENT is not None:
    return _CACHED_CLIENT
  
  env_prefix = CURRENT_ENV

  api_key = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_API_KEY')
  account_id = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_ACCOUNT')
  endpoint_url = anvil.secrets.get_secret(f'{env_prefix}_ENDPOINT_URL').rstrip('/')

  if not api_key or not account_id:
    raise ValueError(f"Missing API Credentials for {env_prefix}")

  return TradierAPI(token=api_key, default_account_id=account_id, endpoint=endpoint_url)

# --- ENVIRONMENT & MARKET STATUS ---

def get_environment_status() -> dict:
  """
    Checks market clock and returns operational status.
    """
  t = _get_client()
  now = dt.datetime.now()

  status_data = {
    'status': 'CLOSED',
    'status_message': 'Market is Closed',
    'today': now.date(),
    'now': now,
    'is_holiday': False,
    'current_env': CURRENT_ENV,
    'target_underlying': config.TARGET_UNDERLYING[CURRENT_ENV]
  }

  try:
    # Raw call to avoid wrapper bugs
    # Endpoint: /v1/markets/clock
    response = t.session.get(f"{t.endpoint}/markets/clock", headers={'Accept': 'application/json'})
    if response.status_code == 200:
      clock = response.json().get('clock', {})
      state = clock.get('state')

      if state == 'open':
        status_data['status'] = 'OPEN'
        status_data['status_message'] = 'Market is Open'
      else:
        status_data['status_message'] = f"Market is {state}"

  except Exception as e:
    print(f"API Error checking clock: {e}")
    status_data['status_message'] = f"API Error: {e}"

  return status_data

# --- DATA FETCHING ---

def get_current_positions() -> List[Dict]:
  """
    Fetches raw position list.
    """
  t = _get_client()
  try:
    # Use library helper if it works, or fallback to raw
    # The library .get_positions() is usually fine
    positions = t.get_positions() 
    if not positions:
      return []

      # Normalize to list (Tradier returns dict if single item)
    return positions if isinstance(positions, list) else [positions]

  except Exception as e:
    print(f"API Error fetching positions: {e}")
    return []

def get_market_data_snapshot(cycle) -> Dict:
  """
    Fetches quotes for the cycle's underlying and active options.
    Returns: {'price': 5000.0, 'open': ..., 'hedge_last': ...}
    """
  t = _get_client()
  snapshot = {'price': 0.0, 'open': 0.0, 'previous_close': 0.0}

  # 1. Fetch Underlying Quote
  try:
    # Use your robust legacy method
    quote = _get_quote_direct(t, cycle.underlying)
    if quote:
      last = float(quote.get('last') or 0)
      open_px = float(quote.get('open') or 0)
      prev_close = float(quote.get('prevclose') or 0)
      if last == 0:
        print(f"WARNING: API returned 0 for {cycle.underlying} 'last' price.")

        # If Open is 0 (common in Sandbox), assume Open = Last (No Gap)
      if open_px == 0:
        open_px = last

        # If Prev Close is 0, assume Prev Close = Last (No Gap)
      if prev_close == 0:
        prev_close = last

      snapshot['price'] = last
      snapshot['open'] = open_px
      snapshot['previous_close'] = prev_close

      # Debug Print to confirm what the bot sees
      print(f"Market Data: Last={last} Open={open_px} Prev={prev_close}")
  except Exception as e:
    print(f"Error fetching underlying quote: {e}")

  # 2. Fetch Hedge Quote (if exists)
  hedge = getattr(cycle, 'hedge_trade_link', None)

  if hedge and hasattr(hedge, 'legs') and hedge.legs:
    try:
      # Assume first leg is the long put
      symbol = hedge.legs[0].occ_symbol
      h_quote = _get_quote_direct(t, symbol)
      if h_quote:
        snapshot['hedge_last'] = float(h_quote.get('last') or 0)
    except Exception as e:
      print(f"Error fetching hedge quote: {e}")

  return snapshot

def get_option_chain(date: dt.date, symbol: str = None) -> List[Dict]:
  """
    Fetches chain for a specific date using your resilient legacy parsing.
    If symbol is None, defaults to the current environment's target (SPY/SPX).
    """
  t = _get_client()
  if symbol is None:
    symbol = config.TARGET_UNDERLYING[CURRENT_ENV]
  exp_str = date.strftime('%Y-%m-%d')
  params = {'symbol': symbol, 'expiration': exp_str, 'greeks': 'true'}

  clean_chain = []

  try:
    # Raw GET request
    resp = t.session.get(f"{t.endpoint}/markets/options/chains", params=params, headers={'Accept': 'application/json'})
    data = resp.json()

    # Handle Tradier's messy response structure
    options_list = data.get('options', {}).get('option', [])

    # Normalize to list
    if isinstance(options_list, dict): 
      options_list = [options_list]
    elif options_list == 'null' or options_list is None:
      options_list = []

    for opt in options_list:
      try:
        # Basic validation (Price > 0, Strike Exists)
        if not opt.get('strike') or not opt.get('bid'): 
          continue

          # Ensure floats
        opt['strike'] = float(opt['strike'])
        opt['bid'] = float(opt['bid'])
        opt['ask'] = float(opt['ask'])

        # Parse Greeks (nested or flat depending on Tradier mood)
        # Your logic used 'greeks' key
        greeks = opt.get('greeks', {})
        if greeks:
          opt['delta'] = float(greeks.get('delta', 0))
          # You can add gamma/theta here if needed

        clean_chain.append(opt)

      except (ValueError, TypeError):
        continue

  except Exception as e:
    print(f"API Error fetching chain for {date}: {e}")

  return clean_chain

# --- EXECUTION ---

def open_spread_position(trade_data: Dict, preview: bool=False) -> Dict:
  """
    Submits a multileg order (Vertical Spread).
    Uses your 'build_multileg_payload' logic.
    """
  t = _get_client()
  #short_occ = trade_data['short_leg_data']['symbol']
  underlying = config.TARGET_UNDERLYING[CURRENT_ENV]
  
  # 1. Construct Payload
  legs_list = []

  # Short Leg
  legs_list.append({
    'symbol': trade_data['short_leg_data']['symbol'],
    'side': 'sell_to_open',
    'quantity': str(trade_data['quantity'])
  })

  # Long Leg
  legs_list.append({
    'symbol': trade_data['long_leg_data']['symbol'],
    'side': 'buy_to_open',
    'quantity': str(trade_data['quantity'])
  })

  # Build indexed payload (option_symbol[0], side[0], etc.)
  payload = {
    'class': 'multileg',
    'symbol': underlying, # Underlying
    'duration': 'day',
    'type': 'credit', # Credit Spread
    'price': f"{trade_data['net_credit']:.2f}"
  }
  # Inject Preview Flag
  if preview:
    payload['preview'] = 'true'

  for i, leg in enumerate(legs_list):
    payload[f'option_symbol[{i}]'] = leg['symbol']
    payload[f'side[{i}]'] = leg['side']
    payload[f'quantity[{i}]'] = leg['quantity']

  return _submit_order(t, payload)

def buy_option(leg_data: Dict) -> Dict:
  """Submits a single leg buy order (Long Put Hedge)"""
  t = _get_client()
  underlying = config.TARGET_UNDERLYING[CURRENT_ENV]
  payload = {
    'class': 'option',
    'symbol': underlying,
    'option_symbol': leg_data['symbol'],
    'side': 'buy_to_open',
    'quantity': '1', # TODO: Hardcoded for now, or pass in args
    'type': 'market', # Hedges usually bought at market or slight limit
    'duration': 'day'
  }

  return _submit_order(t, payload)

# --- PRIVATE HELPERS ---

def _submit_order(t: TradierAPI, payload: Dict) -> Dict:
  """
    Raw POST to /accounts/{id}/orders
    Returns normalized execution report.
    """
  url = f"{t.endpoint}/accounts/{t.default_account_id}/orders"

  try:
    print(f"API: Submitting Order -> {payload}")
    resp = t.session.post(url, data=payload, headers={'Accept': 'application/json'})
    if resp.status_code >= 400:
      print(f"API FAILED ({resp.status_code}): {resp.text}")

    resp.raise_for_status()

    data = resp.json()
    order_info = data.get('order', {})

    return {
      'id': str(order_info.get('id')),
      'status': order_info.get('status'),
      'price': float(payload.get('price', 0) or 0), # Estimated fill price
      'time': dt.datetime.now()
    }

  except requests.exceptions.HTTPError as e:
    print(f"API HTTP Error: {e.response.text}")
    raise e
  except Exception as e:
    print(f"API Execution Error: {e}")
    raise e

def _get_quote_direct(t: TradierAPI, symbol: str) -> Optional[Dict]:
  """
    Your robust quote fetcher.
    """
  try:
    params = {'symbols': symbol, 'greeks': 'false'}
    resp = t.session.get(f"{t.endpoint}/markets/quotes", params=params, headers={'Accept': 'application/json'})
    data = resp.json()

    quotes = data.get('quotes', {}).get('quote')
    if isinstance(quotes, list) and quotes:
      return quotes[0]
    elif isinstance(quotes, dict):
      return quotes

    return None
  except Exception:
    return None