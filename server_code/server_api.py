import anvil.email
import anvil.secrets
import anvil.server

import datetime as dt
import requests
from urllib.parse import urljoin
from typing import Dict, List, Any, Optional, Tuple
import time 
import pytz

from tradier_python import TradierAPI
from shared import config
from shared.types import EnvStatus
from . import server_logging as logger

# Global cache variable (starts empty)
_CACHED_CLIENT = None

# --- AUTHENTICATION ---
def _get_client() -> TradierAPI:
  """Returns the cached client if exist or a new authenticated TradierAPI client based on the current environment"""  
  global _CACHED_CLIENT
  if _CACHED_CLIENT is not None:
    return _CACHED_CLIENT
  
  env_prefix = config.ACTIVE_ENV

  api_key = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_API_KEY')
  account_id = anvil.secrets.get_secret(f'{env_prefix}_TRADIER_ACCOUNT')
  endpoint_url = anvil.secrets.get_secret(f'{env_prefix}_ENDPOINT_URL').rstrip('/')

  if not api_key or not account_id:
    raise ValueError(f"Missing API Credentials for {env_prefix}")

  return TradierAPI(token=api_key, default_account_id=account_id, endpoint=endpoint_url)

# --- ENVIRONMENT & MARKET STATUS ---

def get_environment_status() -> EnvStatus:
  """Checks market clock and returns operational status"""
  t = _get_client()
  
  # 1. Get Timezone-Aware UTC
  utc_now = dt.datetime.now(pytz.utc)
  # 2. Convert to US/Eastern
  eastern = pytz.timezone('US/Eastern')
  et_now = utc_now.astimezone(eastern)

  # 3. Strip Timezone Info (Make it Naive)
  # This prevents "can't compare offset-naive and offset-aware" errors downstream
  # and ensures 9:30 AM ET looks like 09:30:00 to the bot.
  wall_clock_now = et_now.replace(tzinfo=None)

  status_data = {
    'status': 'CLOSED',
    'status_message': 'Market is Closed',
    'today': wall_clock_now.date(),
    'now': wall_clock_now,
    'is_holiday': False,
    'next_state_change': '00:00',
    'current_env': config.ACTIVE_ENV,
    'target_underlying': config.TARGET_UNDERLYING[config.ACTIVE_ENV]
  }

  try:
    response = t.session.get(f"{t.endpoint}/markets/clock", headers={'Accept': 'application/json'})
    if response.status_code == 200:
      clock = response.json().get('clock', {})
      state = clock.get('state')
      status_data['next_state_change'] = str(clock.get('next_change', '16:00'))
    
      if state == 'open':
        status_data['status'] = 'OPEN'
        status_data['status_message'] = 'Market is Open'
      else:
        status_data['status_message'] = f"Market is {state}"

  except Exception as e:
    logger.log(f"API Error checking clock: {e}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
    status_data['status_message'] = f"API Error: {e}"

  return status_data

# --- DATA FETCHING ---

def get_current_positions() -> List[Dict]:
  """
    Fetches raw position list and normalizes to standard Dictionaries.
    Handles Pydantic objects returned by the library.
    """
  t = _get_client()
  try:
    raw_positions = t.get_positions()
    if not raw_positions:
      return []

      # 1. Normalize to List
      # Broker returns a single object if only 1 position exists
    pos_list = raw_positions if isinstance(raw_positions, list) else [raw_positions]

    # 2. Normalize Objects to Dicts
    clean_list = []
    for p in pos_list:
      if isinstance(p, dict):
        clean_list.append(p)
      elif hasattr(p, 'dict'):
        # Pydantic V1 support
        clean_list.append(p.dict())
      elif hasattr(p, 'model_dump'):
        # Pydantic V2 support
        clean_list.append(p.model_dump())
      else:
        # Fallback: Extract known attributes manually
        clean_list.append({
          'symbol': getattr(p, 'symbol', None),
          'quantity': getattr(p, 'quantity', 0),
          'id': getattr(p, 'id', None)
        })

    return clean_list

  except Exception as e:
    logger.log(f"API Error fetching positions: {e}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
    return []

def get_market_data_snapshot(cycle) -> Dict:
  """
  Fetches quotes for underlying, hedge, AND active spreads.
  Now includes Greeks for Hedge Maintenance checks.
  """
  t = _get_client()
  snapshot = {
    'price': 0.0, 'open': 0.0, 'previous_close': 0.0, 
    'spread_marks': {}, 
    'hedge_last': 0.0, 
    'hedge_delta': 0.0, 
    'hedge_theta': 0.0, 
    'hedge_dte': 0
  }

  # 2. CRITICAL CHECK: If cycle is None, return empty snapshot immediately
  if not cycle:
    return snapshot
    
  # 1. Collect all symbols needed
  symbols = [cycle.underlying]

  hedge = getattr(cycle, 'hedge_trade_link', None)
  if hedge and hedge.legs:
    symbols.append(hedge.legs[0].occ_symbol)
  income_trades = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]
  for trade in income_trades:
    for leg in trade.legs:
      symbols.append(leg.occ_symbol)

  # 2. Single Multi-Quote Call
  symbol_str = ",".join(list(set(symbols))) # Deduplicate
  params = {'symbols': symbol_str, 'greeks': 'true'}
  resp = t.session.get(f"{t.endpoint}/markets/quotes", params=params, headers={'Accept': 'application/json'})

  # 3. Map Results
  raw_quotes = resp.json().get('quotes', {}).get('quote', [])
  if isinstance(raw_quotes, dict): raw_quotes = [raw_quotes]

  quote_map = {q['symbol']: q for q in raw_quotes}

  def safe_float(val, default=0.0) -> float:
    try:
      if val is None: return default
      return float(val)
    except (ValueError, TypeError):
      return default

  # 4. Extract Underlying
  u_q = quote_map.get(cycle.underlying)
  if u_q:
    last_px = u_q.get('last') or 0
    open_px = u_q.get('open') or last_px or 0
    prev_close_px = u_q.get('prevclose') or last_px or 0
    snapshot['price'] = float(last_px)
    snapshot['open'] = float(open_px)
    snapshot['previous_close'] = float(prev_close_px)

  # 5. Extract Hedge
  if hedge and hedge.legs:
    h_q = quote_map.get(hedge.legs[0].occ_symbol)
    if h_q:
      snapshot['hedge_last'] = (safe_float(h_q.get('bid')) + safe_float(h_q.get('ask'))) / 2.0 or safe_float(h_q.get('last'))
      greeks = h_q.get('greeks')
      if isinstance(greeks, dict):
        snapshot['hedge_delta'] = safe_float(greeks.get('delta'))
        snapshot['hedge_theta'] = abs(safe_float(greeks.get('theta')))
      else:
        snapshot['hedge_delta'] = 0.0
      # Extract DTE
      # 'expiration_date': '2026-01-04'
      exp_str = h_q.get('expiration_date')
      if exp_str:
        exp_date = dt.datetime.strptime(exp_str, "%Y-%m-%d").date()
        snapshot['hedge_dte'] = (exp_date - dt.date.today()).days

  # 6. Extract Spreads
  for trade in income_trades:
    short_leg = next((l for l in trade.legs if l.side == config.LEG_SIDE_SHORT), None)
    long_leg = next((l for l in trade.legs if l.side == config.LEG_SIDE_LONG), None)
    if short_leg and long_leg:
      s_q = quote_map.get(short_leg.occ_symbol)
      l_q = quote_map.get(long_leg.occ_symbol)
      if s_q and l_q:
        snapshot['spread_marks'][trade.id] = float(s_q.get('ask', 0)) - float(l_q.get('bid', 0))

  return snapshot
  
def get_option_chain(date: dt.date, symbol: str = None) -> List[Dict]:
  """
  Fetches chain for a specific date using your resilient legacy parsing.
  If symbol is None, defaults to the current environment's target (SPY/SPX).
  """
  t = _get_client()
  if symbol is None:
    symbol = config.TARGET_UNDERLYING[config.ACTIVE_ENV]
  exp_str = date.strftime('%Y-%m-%d')
  params = {'symbol': symbol, 'expiration': exp_str, 'greeks': 'true'}

  clean_chain = []

  try:
    # Raw GET request
    resp = t.session.get(f"{t.endpoint}/markets/options/chains", params=params, headers={'Accept': 'application/json'})
    data = resp.json()
    if data is None:
      return []
    options_container = data.get('options')
    if options_container is None:
      return []

    options_list = options_container.get('option', [])
    
    # Normalize to list
    if isinstance(options_list, dict): 
      options_list = [options_list]
    elif options_list == 'null' or options_list is None:
      options_list = []

    for opt in options_list:
      try:
        # Basic validation (Price > 0, Strike Exists)

        # only trade SPXW, not SPX
        root = opt.get('root_symbol')
        #if symbol == 'SPX' and root != 'SPXW': continue 
        if not opt.get('strike') or not opt.get('bid'): continue

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
    logger.log(f"API Error fetching chain for {date}: {e}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)

  return clean_chain

def get_expirations(symbol: str = None) -> List[dt.date]:
  """Fetches ALL valid expiration dates for a symbol"""
  t = _get_client()
  if symbol is None: symbol = config.TARGET_UNDERLYING[config.ACTIVE_ENV]

  try:
    # Endpoint: /v1/markets/options/expirations
    params = {'symbol': symbol, 'includeAllRoots': 'true'}
    resp = t.session.get(f"{t.endpoint}/markets/options/expirations", params=params, headers={'Accept': 'application/json'})
    data = resp.json()

    # Handle "expiration" key (could be list or dict)
    if not data or 'expirations' not in data:
      return []

    dates_raw = data['expirations'].get('date', [])

    # Normalize to list
    if isinstance(dates_raw, str): dates_raw = [dates_raw]

    valid_dates = []
    for d_str in dates_raw:
      try:
        valid_dates.append(dt.datetime.strptime(d_str, "%Y-%m-%d").date())
      except ValueError:
        continue

    return sorted(valid_dates)

  except Exception as e:
    logger.log(f"API Error fetching expirations: {e}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
    return []

# --- EXECUTION ---

def open_spread_position(trade_data: Dict, preview: bool=False) -> Dict:
  """
    Submits a multileg order (Vertical Spread).
    Uses your 'build_multileg_payload' logic.
    """
  t = _get_client()
  #short_occ = trade_data['short_leg_data']['symbol']
  underlying = config.TARGET_UNDERLYING[config.ACTIVE_ENV]
  
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
  underlying = config.TARGET_UNDERLYING[config.ACTIVE_ENV]
  limit_price = float(leg_data.get('ask') or leg_data.get('last') or 0)

  if limit_price == 0:
    raise ValueError(f"Cannot buy {leg_data['symbol']} - No Ask price available.")
    
  payload = {
    'class': 'option',
    'symbol': underlying,
    'option_symbol': leg_data['symbol'],
    'side': 'buy_to_open',
    'quantity': '1', # TODO: Hardcoded for now, or pass in args
    'type': 'limit', # Hedges usually bought at market or slight limit
    'price': f"{limit_price:.2f}",
    'duration': 'day'
  }

  return _submit_order(t, payload)

def execute_roll(old_trade, new_short, new_long, net_price: float) -> dict:
  """
    Submits a 4-Leg Order (Iron Condor style logic, effectively).
    Closes Old, Opens New.
    """
  t = _get_client()

  # 1. Identify Old Legs
  old_short = next(l for l in old_trade.legs if l.side == config.LEG_SIDE_SHORT)
  old_long = next(l for l in old_trade.legs if l.side == config.LEG_SIDE_LONG)

  legs_list = []

  # LEG 1: Buy to Close Old Short
  legs_list.append({
    'symbol': old_short.occ_symbol,
    'side': 'buy_to_close',
    'quantity': str(old_trade.quantity)
  })

  # LEG 2: Sell to Close Old Long
  legs_list.append({
    'symbol': old_long.occ_symbol,
    'side': 'sell_to_close',
    'quantity': str(old_trade.quantity)
  })

  # LEG 3: Sell to Open New Short
  legs_list.append({
    'symbol': new_short['symbol'],
    'side': 'sell_to_open',
    'quantity': str(old_trade.quantity)
  })

  # LEG 4: Buy to Open New Long
  legs_list.append({
    'symbol': new_long['symbol'],
    'side': 'buy_to_open',
    'quantity': str(old_trade.quantity)
  })

  # 2. Build Payload
  root = config.TARGET_UNDERLYING[config.ACTIVE_ENV]

  payload = {
    'class': 'multileg',
    'symbol': root,
    'duration': 'day',
    # Net Price: If Positive, we collect credit. If 0.00, it's "even".
    'type': 'credit' if net_price >= 0 else 'debit',
    'price': f"{abs(net_price):.2f}"
  }

  # Sandbox Stability
  if 'sandbox' in t.endpoint:
    payload['type'] = 'market'

  for i, leg in enumerate(legs_list):
    payload[f'option_symbol[{i}]'] = leg['symbol']
    payload[f'side[{i}]'] = leg['side']
    payload[f'quantity[{i}]'] = leg['quantity']

  return _submit_order(t, payload)

def close_position(trade, order_type: str = 'limit') -> Dict:
  """
    Closes a position (Spread or Hedge).
    Dynamically switches between 'option' and 'multileg' endpoints.
    order_type: 'limit' (default, uses target price) or 'market' (for panic).
    """
  t = _get_client()

  # 1. Identify Legs
  legs = getattr(trade, 'legs', [])
  short_leg = next((l for l in legs if l.side == config.LEG_SIDE_SHORT), None)
  long_leg = next((l for l in legs if l.side == config.LEG_SIDE_LONG), None)

  legs_list = []

  if short_leg:
    legs_list.append({
      'symbol': short_leg.occ_symbol,
      'side': 'buy_to_close',
      'quantity': str(trade.quantity)
    })

  if long_leg:
    legs_list.append({
      'symbol': long_leg.occ_symbol,
      'side': 'sell_to_close',
      'quantity': str(trade.quantity)
    })

    # 2. Dynamic Symbol Resolution
  root = config.TARGET_UNDERLYING[config.ACTIVE_ENV]
  check_leg = short_leg or long_leg
  if check_leg:
    if 'SPX' in check_leg.occ_symbol: root = 'SPX'
    if 'SPY' in check_leg.occ_symbol: root = 'SPY'

    # 3. Build Payload
  num_legs = len(legs_list)

  if num_legs == 1:
    # --- SINGLE LEG LOGIC (Hedge) ---
    payload = {
      'class': 'option',
      'symbol': root,
      'duration': 'day',
      'type': 'market', # Always market for single leg close safety
      'option_symbol': legs_list[0]['symbol'],
      'side': legs_list[0]['side'],
      'quantity': legs_list[0]['quantity']
    }

  else:
    # --- MULTI LEG LOGIC (Spread) ---
    payload = {
      'class': 'multileg',
      'symbol': root,
      'duration': 'day'
    }
    if order_type == 'market':
      payload['type'] = 'market'
    else:
    # Determine pricing direction (Debit/Credit) NOT used for Market orders but good practice
      side = 'debit' if trade.role == config.ROLE_INCOME else 'credit'
      payload['type'] = side
      price_val = trade.target_harvest_price if trade.target_harvest_price is not None else 0.00
      payload['price'] = f"{price_val:.2f}" 
        
    # Sandbox Stability
    if 'sandbox' in t.endpoint:
      payload['type'] = 'market'
      if 'price' in payload: del payload['price']

    for i, leg in enumerate(legs_list):
      payload[f'option_symbol[{i}]'] = leg['symbol']
      payload[f'side[{i}]'] = leg['side']
      payload[f'quantity[{i}]'] = leg['quantity']

  # 4. Submit
  result = _submit_order(t, payload)
  return result

def wait_for_order_fill(order_id: str, timeout_seconds: int = 15, fill_px_fallback: float=0.0) -> Tuple[str, float]:
  """
    Polls Tradier for specific order ID until it is 'filled' or timeout occurs.
    Returns (status_string, avg_fill_price).
    status_string values: 'filled', 'canceled', 'rejected', 'expired', 'timeout'
    """
  t = _get_client()
  # Dry Run handling
  if order_id.startswith("DRY_"):
    if "FAIL" in order_id:
      logger.log(f"SIMULATION: Forcing Timeout for order {order_id}", level=config.LOG_WARNING)
      return 'timeout', 0.0
    else:
      logger.log(f"SIMULATION: Auto-filling simulated order {order_id}", 
                level=config.LOG_INFO, 
                source=config.LOG_SOURCE_API)
    return 'filled', fill_px_fallback  # Or pass the price back if you want to test PnL math
    
  url = f"{t.endpoint}/accounts/{t.default_account_id}/orders/{order_id}"
  start_time = time.time()

  while (time.time() - start_time) < timeout_seconds:
    try:
      resp = t.session.get(url, headers={'Accept': 'application/json'})
      if resp.status_code == 200:
        data = resp.json()
        # Tradier structure: {'order': {'status': 'filled', ...}}
        order_data = data.get('order', {})
        reason = order_data.get('reason_description', 'No reason provided')
        status = order_data.get('status')
        if status == 'filled':
          fill_price = float(order_data.get('avg_fill_price') or 0.0)
          logger.log(f"Order {order_id} FILLED at ${fill_price}", 
                     level=config.LOG_INFO, 
                     source=config.LOG_SOURCE_API)
          return 'filled', fill_price

        if status in ['canceled', 'rejected', 'expired']:
          logger.log(f"Order {order_id} died: {status.upper()} - {reason}", 
                     level=config.LOG_WARNING, 
                     source=config.LOG_SOURCE_API)
          return status, 0.0

          # If 'open' or 'pending', wait and retry
      time.sleep(1.0)

    except Exception as e:
      logger.log(f"API Polling Error: {e}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
      time.sleep(1.0)
      
  logger.log(f"Order {order_id} timed out (not filled)", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
  return False, 0.0

def cancel_order(order_id: str) -> bool:
  """
    Cancels a specific order.
    Returns True if successful (or already gone), False if failed.
    """
  t = _get_client()
  url = f"{t.endpoint}/accounts/{t.default_account_id}/orders/{order_id}"

  try:
    logger.log(f"Canceling Order {order_id}...", level=config.LOG_INFO, source=config.LOG_SOURCE_API)
    resp = t.session.delete(url, headers={'Accept': 'application/json'})

    # 200 OK means successfully cancelled
    if resp.status_code == 200:
      print("API: Cancel successful.")
      return True

    logger.log(f"Cancel failed code {resp.status_code}: {resp.text}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
    return False

  except Exception as e:
    logger.log(f"API Error canceling order: {e}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
    return False
    
# --- PRIVATE HELPERS ---

def _submit_order(t: TradierAPI, payload: Dict) -> Dict:
  """
    Raw POST to /accounts/{id}/orders
    Returns normalized execution report.
    """
  if config.DRY_RUN:
    logger.log(f"DRY RUN: Order Suppressed -> {payload}", 
               level=config.LOG_WARNING, 
               source=config.LOG_SOURCE_API)
    return {
      'id': f"DRY_{dt.datetime.now().strftime('%H%M%S')}",
      'status': 'filled',
      'price': float(payload.get('price', 0) or 0),
      'time': dt.datetime.now()
    }
    
  url = f"{t.endpoint}/accounts/{t.default_account_id}/orders"

  try:
    logger.log(f"Submitting Order -> {payload}", level=config.LOG_INFO, source=config.LOG_SOURCE_API)
    resp = t.session.post(url, data=payload, headers={'accept': 'application/json'})
    if resp.status_code == 500 and "sandbox" in t.endpoint:
      logger.log("WARNING: Tradier Sandbox 500 Error (Known Glitch). Bypassing...", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
      return {
        'id': f"FAKE_{dt.datetime.now().strftime('%H%M%S')}",
        'status': 'filled', # Pretend it filled
        'price': float(payload.get('price', 0) or 0),
        'time': dt.datetime.now()
      }
      # --- SANDBOX BYPASS END ---
    if resp.status_code >= 400:
      logger.log(f"API FAILED ({resp.status_code}): {resp.text}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)

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
    logger.log(f"API HTTP Error: {e.response.text}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
    raise e
  
  except Exception as e:
    logger.log(f"API Execution Error: {e}", level=config.LOG_WARNING, source=config.LOG_SOURCE_API)
    raise e

def _get_quote_direct(t: TradierAPI, symbol: str, greeks: bool=False) -> Optional[Dict]:
  """Your robust quote fetcher"""
  try:
    params = {'symbols': symbol, 'greeks': str(greeks).lower()}
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