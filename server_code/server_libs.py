from shared import config
from shared import types
from shared.classes import Cycle, Trade
from shared.types import MarketData

import datetime as dt
from typing import Optional

def can_run_automation(env, cycle: Cycle):
  # STUB: Always say yes for testing
  return True

def is_db_consistent(cycle: Cycle, positions):
  # STUB: Assume DB is perfect
  return True

def determine_cycle_state(cycle: Cycle, market_data: types.MarketData)->str:
  """
  The "Policy Manager".
  It runs checks in priority order. The first one to return a state wins.
  """
  # 1. CRITICAL: Panic Harvest (Protect Capital)
  # Priority: High. If the house is burning, get out.
  if _check_panic_harvest(cycle, market_data):
    return config.STATE_PANIC_HARVEST

  # 2. DEFENSIVE: Roll Logic (Protect the Spread)
  # Priority: High. If spread is threatened, move it.
  if _check_roll_needed(cycle, market_data):
    return config.STATE_ROLL_REQUIRED

  # 3. MAINTENANCE: Hedge Health (Protect the Shield)
  # Priority: Medium. If the airbag is broken, fix it before driving.
  if _check_hedge_maintenance(cycle, market_data):
    return config.STATE_HEDGE_ADJUSTMENT_NEEDED

  # 4. OFFENSIVE: Profit Taking (Harvest Income)
  # Priority: Medium. Secure the bag.
  if _check_profit_target(cycle, market_data):
    return config.STATE_HARVEST_TARGET_HIT

  # 5. SETUP: Structure Checks (Build the Position)
  # Priority: Low. Only happens if we are safely idle.
  if _check_hedge_missing(cycle):
    return config.STATE_HEDGE_MISSING

  if _check_spread_missing(cycle):
    return config.STATE_SPREAD_MISSING

  # 6. DEFAULT
  return config.STATE_IDLE

def get_threatened_spread(cycle: Cycle, market_data: dict) -> Optional[Trade]:
  """
  Returns the specific trade that needs rolling.
  """
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      trigger = trade.roll_trigger_price

      if current_cost and trigger and current_cost >= trigger:
        return trade
  return None

def calculate_roll_legs(
  chain: list[dict],
  current_short_strike: float,
  width: float,
  cost_to_close: float
) -> dict | None:
  """
  Scans for a 'Down & Out' roll.
  Maximizes distance by finding the lowest strike that generates sufficient credit
  """
  #print(f"DEBUG: Entering calculate_roll_legs_v3. Chain Size: {len(chain)}")

  # Filter for Puts
  puts = [o for o in chain if o.get('option_type') == 'put']
  if not puts: return None

  # Sort by Strike (High to Low)
  puts.sort(key=lambda x: x['strike'], reverse=True)

  best_candidate = None

  def get_price(opt, side):
    val = opt.get(side)
    if val is None or val == 0: val = opt.get('last', 0)
    return float(val)

  #print(f"DEBUG ROLL: Scanning {len(puts)} puts. Current Short: {current_short_strike}.")
  for short_candidate in puts:
    short_strike = short_candidate['strike']
    if short_strike >= current_short_strike:
      continue

    # Find matching Long Leg
    target_long = short_strike - width
    long_candidate = next((p for p in puts if abs(p['strike'] - target_long) < 0.05), None)
    if not long_candidate:
      continue

    # Calculate Economics
    credit_new = get_price(short_candidate, 'bid') - get_price(long_candidate, 'ask')
    if credit_new >= cost_to_close:
      # This is a valid escape. OVERWRITE the last best one.
      # By the end of the loop, this will hold the LOWEST strike that was valid.
      best_candidate = {
        'short_leg': short_candidate,
        'long_leg': long_candidate,
        'new_credit': credit_new,
        'net_price': credit_new - cost_to_close
      }
    else:
      # If we were previously finding winners, but now the credit is too low,
      # we can stop because lower strikes will pay even less.
      if best_candidate:  break
        
  return best_candidate

# --- ATOMIC PREDICATE FUNCTIONS ---

def _check_panic_harvest(cycle: Cycle, market_data: MarketData)->bool:
  """Rule: If Net Unit PnL (Hedge Daily Gain + Spread PnL) > Panic Threshold ($350)"""
  if not cycle.hedge_trade_link:
    return False

  # 1. Calculate Hedge PnL (Daily)
  # Daily Gain = Current Price - Daily Reference Price
  hedge_current = market_data.get('hedge_last', 0.0)
  hedge_ref = cycle.daily_hedge_ref or 0.0

  if hedge_ref == 0: 
    return False # Cannot calculate without reference

  hedge_pnl = (hedge_current - hedge_ref) * config.DEFAULT_MULTIPLIER * cycle.hedge_trade_link.quantity

  # 2. Calculate Spread PnL (Total Open)
  # Spread PnL = (Entry Credit - Current Debit) * Qty * 100
  spread_pnl = 0.0
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_debit = marks.get(trade.id, 0.0)
      entry_credit = trade.entry_price or 0.0
      trade_pnl = (entry_credit - current_debit) * config.DEFAULT_MULTIPLIER * trade.quantity
      spread_pnl += trade_pnl

  # 3. Check Threshold
  net_unit_pnl = hedge_pnl + spread_pnl

  # Threshold logic (Scale for SPY if needed)
  threshold = cycle.rules['panic_threshold_dpu'] * cycle.hedge_trade_link.quantity

  # DEBUG LOG (Optional)
  print(f"DEBUG PANIC: Net PnL ${net_unit_pnl:.2f} vs Threshold ${threshold:.2f}")

  return net_unit_pnl > threshold

def _check_roll_needed(cycle: Cycle, market_data: MarketData)->bool:
  """Rule: If Spread Ask Price > 3.0 * Initial Credit"""
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      trigger_price = trade.roll_trigger_price

      # --- DEBUG START ---
      print(f"DEBUG ROLL: Trade {trade.id} Cost={current_cost} Trigger={trigger_price}")
      # --- DEBUG END ---

      # If price is valid and exceeds trigger
      if current_cost is not None and trigger_price and current_cost >= trigger_price:
        return True

  return False

def _check_hedge_maintenance(cycle: Cycle, market_data: MarketData)->bool:
  """Rule: If Hedge Delta < 15 or > 40, OR DTE < 60"""
  # STUB
  if not cycle.hedge_trade_link:
    return False
  # Logic: Check delta/dte from market_data['hedge_greeks']
  return False

def _check_profit_target(cycle: Cycle, market_data: MarketData)->bool:
  """Rule: If Spread Profit >= 50% of max profit"""
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)

      # If we have a valid price and it is CHEAPER than our target debit
      # Example: Target 0.15. Current Cost 0.06.  0.06 <= 0.15 -> TRUE.
      if current_cost is not None and current_cost <= (trade.target_harvest_price or 0):
        return True
  return False

def _check_hedge_missing(cycle: Cycle)->bool:
  """Rule: Cycle is OPEN but has no active Hedge Trade linked"""
  return cycle.hedge_trade_link is None

def _check_spread_missing(cycle: Cycle)->bool:
  """Rule: Hedge exists, but no active Income Trade (Spread) exists."""
  # We check if there are any active trades tagged as 'INCOME'
  # cycle.spread_trades is populated by server_db.get_active_cycle
  open_spreads = [
    t for t in cycle.trades 
    if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN
  ]
  # If we have 0 open spreads, we are "missing" the spread
  return len(open_spreads) == 0

def alert_human(message, level=config.ALERT_INFO):
  print(f"ALERT [{level}]: {message}")

# ... Add other stubs (select_hedge_strike, calculate_roll_legs) as we hit them
# in chronological order of usage

def check_entry_conditions(
  current_price: float,
  open_price: float,
  previous_close: float,
  current_time: dt.datetime,
  rules: dict
) -> tuple[bool, str]:

  # 1. Time Check: Ensure minimum minutes after open have passed
  market_open_dt = dt.datetime.combine(current_time.date(), config.MARKET_OPEN_TIME)
  minutes_since_open = (current_time - market_open_dt).total_seconds() / 60.0

  if minutes_since_open < rules['trade_start_delay']:
    return False, "Wait time active"

    # 2. Overnight Gap Check: Did market open significantly lower?
  overnight_drop_pct = (open_price - previous_close) / previous_close
  if overnight_drop_pct < -rules['gap_down_thresh']:
    return False, f"Overnight gap {overnight_drop_pct:.1%} exceeds limit of {rules['gap_down_thresh']*100}%"

    # 3. Intraday Drop Check: Has market crashed since open?
  intraday_drop_pct = (current_price - open_price) / open_price
  if intraday_drop_pct < -rules['gap_down_thresh']:
    return False, f"Intraday drop {intraday_drop_pct:.1%} exceeds limit"

  return True, "Entry valid"

def select_hedge_strike(chain: list[dict], target_delta: float = 0.25) -> dict | None:
  """Finds the Put option closest to the target delta (e.g., 0.25)"""
  # 1. Filter for Puts
  puts = [opt for opt in chain if opt['option_type'] == config.TRADIER_OPTION_TYPE_PUT]
  if not puts:
    return None

  # 2. Find Closest Delta
  def get_delta(opt):
    greeks = opt.get('greeks')
    return abs(greeks.get('delta', 0.0)) if greeks else 0.0
  # Note: Puts have negative delta, so we look for abs(delta) ~ 0.25
  # Example: -0.25 target.
  best_leg = min(puts, key=lambda x: abs(get_delta(x) - target_delta))

  return best_leg

def get_target_hedge_date(cycle: Cycle, current_date:Optional[dt.date]=None)->dt.date:
  """Calculates the target expiration date based on the Cycle's RuleSet."""
  if not current_date:
    current_date = dt.datetime.now().date()
  target_days = cycle.rule_set._row['hedge_target_dte']
  return current_date + dt.timedelta(days=target_days)

def calculate_spread_strikes(
  chain: list[dict],
  target_delta: float,
  spread_width: float,
  option_type: str = config.TRADIER_OPTION_TYPE_PUT
) -> tuple[float, float] | None:
  """
    Scans the chain for the strike closest to target_delta.
    Verifies the protection strike (width) exists.
    Returns (short_strike, long_strike) or None.
    """
  # 1. Filter for the requested side (put/call)
  side_chain = [opt for opt in chain if opt['option_type'] == option_type]

  if not side_chain:
    return None

  # Helper for safe delta access
  def get_delta(opt):
    greeks = opt.get('greeks')
    return abs(greeks.get('delta', 0.0)) if greeks else 0.0
    
  # 2. Find Short Strike (Closest to Target Delta)
  # We use abs() so 0.25 target matches -0.25 delta on puts
  short_leg = min(side_chain, key=lambda x: abs(get_delta(x) - target_delta))
  short_strike = short_leg['strike']

  # 3. Calculate Target Long Strike
  if option_type == config.TRADIER_OPTION_TYPE_PUT:
    long_strike = short_strike - spread_width
  else: # call
    long_strike = short_strike + spread_width

  print(f"DEBUG: Short Strike: {short_strike} (Delta {get_delta(short_leg):.2f})")
  print(f"DEBUG: Target Width: {spread_width} -> Looking for Long Strike: {long_strike}")
  
    # 4. Verify Long Strike Exists in the Data
    # Exact match required to maintain strict risk profile
  long_leg = next((opt for opt in side_chain if opt['strike'] == long_strike), None)

  if long_leg:
    return short_strike, long_strike

  # --- DEBUG START ---
  print("DEBUG: Long Strike NOT found in chain.")
  # Optional: Print nearby strikes to see what WAS available
  nearby = [o['strike'] for o in side_chain if abs(o['strike'] - long_strike) < 5]
  print(f"DEBUG: Nearby strikes: {nearby}")
  # --- DEBUG END ---
  
  return None

def validate_premium_and_size(
  short_leg: dict,
  long_leg: dict,
  rules: dict
) -> tuple[bool, float, str]:
  """
    Calculates spread mid-price, validates against rules, and determines size.
    Returns: (is_valid, quantity, net_credit, message)
    """
  # 1. Calculate Mid Prices
  short_mid = (short_leg['bid'] + short_leg['ask']) / 2.0
  long_mid = (long_leg['bid'] + long_leg['ask']) / 2.0

  # 2. Calculate Net Credit (Short - Long)
  net_credit = short_mid - long_mid

  # 3. Validate against RuleSet limits
  if net_credit < rules['spread_min_premium']:
    return False, net_credit, f"Credit {net_credit:.2f} below min {rules['spread_min_premium']}"

  if net_credit > rules['spread_max_premium']:
    return False, net_credit, f"Credit {net_credit:.2f} exceeds max {rules['spread_max_premium']}"

  return True, net_credit, "Premium Valid"

def get_spread_quantity(
  hedge_quantity: int,
  spread_price: float,
  rules: dict
) -> int:
  """
    Calculates trade size based on hedge ratio and spread price.
    Formula: (Hedge_Qty * Size_Factor) / Spread_Price
    """
  if spread_price <= 0:
    return 0
  raw_qty = int(hedge_quantity * rules['spread_size_factor'] / spread_price)
  return raw_qty

def evaluate_entry(
  cycle: Cycle,
  current_time: dt.datetime,
  current_price: float,
  open_price: float,
  previous_close: float,
  option_chain: list[dict],
  rules: dict
) -> tuple[bool, dict, str]:
  """
    Master function to check ALL entry conditions.
    Returns: (is_valid, trade_data_dict, reason_message)
    """

  # 1. Broad Market Checks (Time, Gaps)
  # We pass the full rules dict as requested
  valid_market, market_msg = check_entry_conditions(
    current_price=current_price,
    open_price=open_price,
    previous_close=previous_close,
    current_time=current_time,
    rules=rules
  )

  if not valid_market:
    return False, {}, market_msg

    # 2. Strike Selection
  strikes = calculate_spread_strikes(
    chain=option_chain,
    target_delta=rules['spread_target_delta'],
    spread_width=rules['spread_width'],
    option_type="put" # Hardcoded for Put Credit Spread strategy
  )

  if not strikes:
    return False, {}, "Could not find valid strikes (Check Delta/Width)"

  short_strike, long_strike = strikes

  # 3. Fetch Leg Data for Pricing
  # Helper to find the specific leg dicts again (optimized in real implementation)
  short_leg = next(opt for opt in option_chain if opt['strike'] == short_strike and opt['option_type'] == 'put')
  long_leg = next(opt for opt in option_chain if opt['strike'] == long_strike and opt['option_type'] == 'put')

  # 4. Premium Validation
  valid_prem, net_credit, prem_msg = validate_premium_and_size(
    short_leg=short_leg,
    long_leg=long_leg,
    rules=rules
  )

  if not valid_prem:
    return False, {}, prem_msg

    # 5. Sizing Calculation
    # We need the hedge quantity. If no hedge trade exists, we can default to 0 or block.
    # Assuming cycle.hedge_trade is populated if status is Active.
  hedge_qty = cycle.hedge_trade.quantity if cycle.hedge_trade else 0

  if hedge_qty == 0:
    return False, {}, "No active hedge quantity found to size against"

  quantity = get_spread_quantity(
    hedge_quantity=hedge_qty,
    spread_price=net_credit,
    rules=rules
  )

  if quantity == 0:
    return False, {}, "Calculated quantity is 0 (Price too high?)"

    # 6. Success - Package the Data
  trade_data = {
    'short_strike': short_strike,
    'long_strike': long_strike,
    'net_credit': net_credit,
    'quantity': quantity,
    'short_leg_data': short_leg, # Passing full leg data for ID tracking later
    'long_leg_data': long_leg
  }

  return True, trade_data, "Entry Valid"

def get_winning_spread(cycle: Cycle, market_data: MarketData) -> Optional[Trade]:
  """
    Returns the specific trade that hit its profit target.
    """
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      if current_cost is not None and current_cost <= (trade.target_harvest_price or 0):
        return trade
  return None
