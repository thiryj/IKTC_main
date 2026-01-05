from shared import config
from shared.classes import Cycle, Trade
from shared.types import MarketData
from typing import Optional, Tuple, Dict, List
import datetime as dt

# --- ORCHESTRATION HELPERS ---

def can_run_automation(env_status: dict, settings:dict) -> bool:
  """Checks if the bot is allowed to run based on Market Status and Settings"""
  if not settings or not settings.get('automation_enabled'): 
    return False
    
  # 1. Market Hours Check
  if env_status.get('status') != 'OPEN':
    return False
  
  return True

def is_db_consistent(cycle: Optional[Cycle], positions: List[dict]) -> bool:
  # TODO: Implement reconciliation logic.
  # Compare cycle.trades vs Tradier positions. Return False if mismatch found.
  return True

def determine_cycle_state(cycle: Cycle, market_data: MarketData) -> str:
  """
  The "Policy Manager". Checks conditions in priority order.
  """
  # 1. CRITICAL: Panic Harvest (Protect Capital)
  if _check_panic_harvest(cycle, market_data):
    return config.STATE_PANIC_HARVEST

  # 2. DEFENSIVE: Roll Logic (Protect the Spread)
  if _check_roll_needed(cycle, market_data):
    return config.STATE_ROLL_REQUIRED

  # 3. MAINTENANCE: Hedge Health
  if _check_hedge_maintenance(cycle, market_data):
    return config.STATE_HEDGE_ADJUSTMENT_NEEDED

  # 4. OFFENSIVE: Profit Taking
  if _check_profit_target(cycle, market_data):
    return config.STATE_HARVEST_TARGET_HIT

  # 5. SETUP: Structure Checks
  if _check_hedge_missing(cycle):
    return config.STATE_HEDGE_MISSING

  if _check_spread_missing(cycle):
    return config.STATE_SPREAD_MISSING

  return config.STATE_IDLE

# --- STATE CHECK FUNCTIONS ---

def _check_panic_harvest(cycle: Cycle, market_data: MarketData) -> bool:
  """
  Rule: Net Unit PnL (Hedge Gain + Spread PnL) > Panic Threshold.
  """
  if not cycle.hedge_trade_link:
    return False

  # 1. Calculate Hedge PnL (Daily Change)
  # Logic: (Current Price - Daily Reference) * Multiplier * Qty
  hedge_current = market_data.get('hedge_last', 0.0)
  hedge_ref = cycle.daily_hedge_ref or 0.0

  if hedge_ref == 0: 
    # TODO: Handle case where daily_hedge_ref is missing (maybe fetch yesterday's close?)
    return False 

  hedge_pnl = (hedge_current - hedge_ref) * config.DEFAULT_MULTIPLIER * cycle.hedge_trade_link.quantity

  # 2. Calculate Spread PnL (Total Open)
  # Logic: (Entry Credit - Current Debit) * Multiplier * Qty
  spread_pnl = 0.0
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_debit = marks.get(trade.id, 0.0)
      entry_credit = trade.entry_price or 0.0
      trade_pnl = (entry_credit - current_debit) * config.DEFAULT_MULTIPLIER * trade.quantity
      spread_pnl += trade_pnl

  # 3. Check Threshold (Scaled by Rules)
  net_unit_pnl = hedge_pnl + spread_pnl

  # Note: panic_threshold_dpu is already scaled for SPY in cycle.rules
  threshold = cycle.rules['panic_threshold_dpu'] * cycle.hedge_trade_link.quantity

  return net_unit_pnl > threshold

def _check_roll_needed(cycle: Cycle, market_data: MarketData) -> bool:
  """
  Rule: Spread Ask Price > Roll Trigger Price (3x Credit).
  """
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      trigger = trade.roll_trigger_price

      if current_cost and trigger and current_cost >= trigger:
        return True
  return False

def _check_hedge_maintenance(cycle: Cycle, market_data: MarketData) -> bool:
  """Rule: If Hedge Delta < 15 or > 40, OR DTE < 60"""
  if not cycle.hedge_trade_link:
    return False

  # 1. Check Time (DTE)
  current_dte = market_data.get('hedge_dte', 999)
  min_dte = cycle.rules.get('hedge_min_dte', 60)
  if current_dte < min_dte:
    print(f"DEBUG: Hedge DTE {current_dte} < Limit {min_dte}")
    return True

  # 2. Check Delta
  # Puts have negative delta, use ABS
  # Handle Sandbox/Data Glitch where delta is exactly 0.0
  raw_delta = market_data.get('hedge_delta')
  # FIX: If delta is 0 or None, assume data is stale and DO NOT roll.
  if not raw_delta: 
    # print("DEBUG: Hedge delta 0 or missing. Skipping check.")
    return False
  current_delta = abs(raw_delta)
  min_delta = cycle.rules.get('hedge_min_delta', 0.15)
  max_delta = cycle.rules.get('hedge_max_delta', 0.40)
  if current_delta < min_delta or current_delta > max_delta:
    print(f"DEBUG: Hedge Delta {current_delta} out of bounds ({min_delta}-{max_delta})")
    return True

  return False

def _check_profit_target(cycle: Cycle, market_data: MarketData) -> bool:
  """
  Rule: Spread Cost <= Target Harvest Price (50% of credit).
  """
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      target = trade.target_harvest_price

      if current_cost is not None and target and current_cost <= target:
        return True
  return False

def _check_hedge_missing(cycle: Cycle) -> bool:
  return cycle.hedge_trade_link is None

def _check_spread_missing(cycle: Cycle) -> bool:
  open_spreads = [
    t for t in cycle.trades 
    if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN
  ]
  return len(open_spreads) == 0

def alert_human(message: str, level: str = config.ALERT_INFO):
  # TODO: Connect this to email/SMS notification service
  print(f"ALERT [{level}]: {message}")

# --- OBJECT RETRIEVAL HELPERS ---

def get_threatened_spread(cycle: Cycle, market_data: MarketData) -> Optional[Trade]:
  marks = market_data.get('spread_marks', {})
  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      trigger = trade.roll_trigger_price
      if current_cost and trigger and current_cost >= trigger:
        return trade
  return None

def get_winning_spread(cycle: Cycle, market_data: MarketData) -> Optional[Trade]:
  marks = market_data.get('spread_marks', {})
  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      target = trade.target_harvest_price
      if current_cost is not None and target and current_cost <= target:
        return trade
  return None

# --- CALCULATION LOGIC (ROLLS & ENTRY) ---
def find_closest_expiration(valid_dates: List[dt.date], target_dte: int) -> Optional[dt.date]:
  """Given a list of valid dates, finds the one closest to Today + Target DTE"""
  if not valid_dates: 
    return None

  today = dt.date.today()
  target_date = today + dt.timedelta(days=target_dte)

  return min(valid_dates, key=lambda d: abs((d - target_date).days))

def calculate_roll_legs(
  chain: List[Dict],
  current_short_strike: float,
  width: float,
  cost_to_close: float
) -> Optional[Dict]:
  """
    Scans for a 'Down & Out' roll. Maximizes distance.
    Finds the lowest strike that still generates enough credit to pay for 'cost_to_close'.
    """
  # 1. Filter and Sort
  puts = [o for o in chain if o.get('option_type') == 'put']
  if not puts: return None

    # Sort High to Low so we can find the "Lowest Valid" strike
  puts.sort(key=lambda x: x['strike'], reverse=True)

  best_candidate = None

  def get_price(opt, side):
    val = opt.get(side)
    if val is None or val == 0: val = opt.get('last', 0)
    return float(val)

    # 2. Scan
  for short_candidate in puts:
    short_strike = short_candidate['strike']

    # Constraint: Lower than current (Down)
    if short_strike >= current_short_strike: continue

      # Find matching Long Leg
    target_long = short_strike - width

    # Fuzzy match to handle floating point issues
    long_candidate = next((p for p in puts if abs(p['strike'] - target_long) < 0.05), None)

    if not long_candidate: continue

      # Economics
    credit_new = get_price(short_candidate, 'bid') - get_price(long_candidate, 'ask')

    if credit_new >= cost_to_close:
      # Valid candidate found. Store it.
      # We continue the loop to see if a LOWER strike (safer) also pays enough.
      best_candidate = {
        'short_leg': short_candidate,
        'long_leg': long_candidate,
        'new_credit': credit_new,
        'net_price': credit_new - cost_to_close
      }
    else:
      # Credit dropped too low. Since premiums drop as strikes drop,
      # no further candidates will work. Stop scanning.
      if best_candidate: break

  return best_candidate

def check_entry_conditions(
  current_price: float,
  open_price: float,
  previous_close: float,
  current_time: dt.datetime,
  rules: Dict
) -> Tuple[bool, str]:

  # 1. Time Check
  market_open_dt = dt.datetime.combine(current_time.date(), config.MARKET_OPEN_TIME)
  minutes_since_open = (current_time - market_open_dt).total_seconds() / 60.0

  if minutes_since_open < rules['trade_start_delay']:
    return False, "Wait time active"

  # 2. Overnight Gap Check
  if previous_close > 0:
    overnight_drop_pct = (open_price - previous_close) / previous_close
    if overnight_drop_pct < -rules['gap_down_thresh']:
      return False, f"Overnight gap {overnight_drop_pct:.1%} exceeds limit"

  # 3. Intraday Drop Check
  if open_price > 0:
    intraday_drop_pct = (current_price - open_price) / open_price
    if intraday_drop_pct < -rules['gap_down_thresh']:
      return False, f"Intraday drop {intraday_drop_pct:.1%} exceeds limit"

  # TODO: Add VIX check if required by strategy

  return True, "Entry valid"

def get_target_hedge_date(cycle: Cycle, current_date: Optional[dt.date] = None) -> dt.date:
  if not current_date: current_date = dt.datetime.now().date()
  target_days = cycle.rules['hedge_target_dte']
  return current_date + dt.timedelta(days=target_days)

def select_hedge_strike(chain: List[Dict], target_delta: float = 0.25) -> Optional[Dict]:
  puts = [opt for opt in chain if opt['option_type'] == config.TRADIER_OPTION_TYPE_PUT]
  if not puts: return None

  def get_delta(opt):
    greeks = opt.get('greeks')
    return abs(greeks.get('delta', 0.0)) if greeks else 0.0

    # Logic: Minimize distance to target delta
  return min(puts, key=lambda x: abs(get_delta(x) - target_delta))

def calculate_spread_strikes(
  chain: List[Dict],
  target_delta: float,
  spread_width: float,
  option_type: str = config.TRADIER_OPTION_TYPE_PUT
) -> Optional[Tuple[float, float]]:

  side_chain = [opt for opt in chain if opt['option_type'] == option_type]
  if not side_chain: return None

  def get_delta(opt):
    greeks = opt.get('greeks')
    return abs(greeks.get('delta', 0.0)) if greeks else 0.0

  short_leg = min(side_chain, key=lambda x: abs(get_delta(x) - target_delta))
  short_strike = short_leg['strike']

  if option_type == config.TRADIER_OPTION_TYPE_PUT:
    long_strike = short_strike - spread_width
  else:
    long_strike = short_strike + spread_width

  # Verify exact match for long strike exists
  long_leg = next((opt for opt in side_chain if opt['strike'] == long_strike), None)

  if long_leg:
    return short_strike, long_strike

  return None

def validate_premium_and_size(
  short_leg: Dict,
  long_leg: Dict,
  rules: Dict
) -> Tuple[bool, float, str]:

  # Logic: Calculate Credit using Mids (Fallback to Last if 0)
  s_mid = (short_leg.get('bid',0) + short_leg.get('ask',0)) / 2.0
  l_mid = (long_leg.get('bid',0) + long_leg.get('ask',0)) / 2.0

  if s_mid == 0: s_mid = float(short_leg.get('last', 0))
  if l_mid == 0: l_mid = float(long_leg.get('last', 0))

  net_credit = s_mid - l_mid

  if net_credit < rules['spread_min_premium']:
    return False, net_credit, f"Credit {net_credit:.2f} below min {rules['spread_min_premium']}"

  if net_credit > rules['spread_max_premium']:
    return False, net_credit, f"Credit {net_credit:.2f} exceeds max {rules['spread_max_premium']}"

  return True, net_credit, "Premium Valid"

def get_spread_quantity(
  hedge_quantity: int,
  spread_price: float,
  rules: Dict
) -> int:
  """Calculates position size using '5/C' rule (or scaled equivalent)"""
  if spread_price <= 0: return 0

  raw_qty = int(round(hedge_quantity * rules['spread_size_factor'] / spread_price))

  # Apply Safety Cap
  capped_qty = min(raw_qty, hedge_quantity * config.MAX_SPREAD_TO_HEDGE_RATIO)
  return max(1, capped_qty)

def evaluate_entry(
  cycle: Cycle,
  chain: List[Dict],
  current_price: float,
  open_price: float,
  previous_close: float,
  current_time: dt.datetime,
  rules: Dict
) -> Tuple[bool, Dict, str]:
  """
  Master entry function.
  Orchestrates: Conditions -> Strikes -> Validation -> Sizing.
  """

  # 1. Broad Market Checks
  is_valid_env, env_reason = check_entry_conditions(
    current_price, open_price, previous_close, current_time, rules
    )
  if not is_valid_env:
      return False, {}, env_reason

  # 2. Prerequisites
  if not cycle.hedge_trade_link:
      return False, {}, "No active hedge linked to cycle"

  # 3. Strike Selection
  strikes = calculate_spread_strikes(
      chain,
      target_delta=rules['spread_target_delta'],
      spread_width=rules['spread_width']
  )
  
  if not strikes:
      return False, {}, "Could not find valid strikes (Check Delta/Width)"

  short_strike, long_strike = strikes

  # 4. Data Extraction
  short_leg = next(l for l in chain if l['strike'] == short_strike and l['option_type'] == 'put')
  long_leg = next(l for l in chain if l['strike'] == long_strike and l['option_type'] == 'put')

  # 5. Validation
  is_valid_prem, credit, prem_msg = validate_premium_and_size(
      short_leg, long_leg, rules
  )

  if not is_valid_prem:
      return False, {}, f"Strikes {short_strike}/{long_strike} rejected: {prem_msg}"

  # 6. Sizing
  qty = get_spread_quantity(
      hedge_quantity=cycle.hedge_trade_link.quantity,
      spread_price=credit,
      rules=rules
  )

  trade_data = {
      'short_strike': short_strike,
      'long_strike': long_strike,
      'net_credit': credit,
      'quantity': qty,
      'short_leg_data': short_leg,
      'long_leg_data': long_leg
  }
  
  return True, trade_data, "Entry Valid"
