import anvil.email
from anvil.tables import app_tables
from shared import config
from shared.classes import Cycle, Trade
from shared.types import MarketData, EnvStatus, RuleSetDict
from typing import Optional, Tuple, Dict, List
import datetime as dt
import pytz

from . import server_logging as logger

# --- ORCHESTRATION HELPERS ---

def can_run_automation(env_status: dict, settings:dict, EOD_overide: bool = False) -> bool:
  """Checks if the bot is allowed to run based on Market Status and Settings"""
  if config.IGNORE_SCHEDULUED_TASKS:
    return False
  print(f'settings: {settings}')  
  if not settings or not settings.get('automation_enabled'): 
    #print('not settings branch')
    return False

  if EOD_overide:
    return True
    
  if config.ENFORCE_TRADING_HOURS and env_status.get('status') != 'OPEN':
      return False
    
  return True

def is_db_consistent(cycle: Optional[Cycle], positions: List[dict]) -> bool:
  # TODO: Implement reconciliation logic.
  # Compare cycle.trades vs Tradier positions. Return False if mismatch found.
  return True

# In server_libs.py

def determine_scalpel_state(cycle: Cycle, env_status: EnvStatus) -> str:
  """
    Determines the operational phase based on Time and Position status.
      Priorities:
  1. Market Closed? -> Cleanup
  2. Active Position? -> Hunt for $3.50
  3. 3:00 PM - 3:10 PM? -> Entry Window
  4. Before 3:00 PM? -> Waiting
  5. Default -> Idle (e.g. 3:15 PM and flat)
    """
  # 1. Prepare Time Variables
  # Converts 3:05 PM to integer 1505
  now_time = int(env_status['now'].strftime('%H%M'))
  entry_start = int(cycle.rules.get('entry_time_est', 1500))
  entry_end = entry_start + 10 # 10-minute window (e.g., 1510)
  market_close = 1600

  # 2. Check Database for Open Trades
  open_trades = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]
  has_active_trade = len(open_trades) > 0

  # --- PRIORITY 1: End of Day Cleanup ---
  if now_time >= market_close:
    return config.STATE_EOD_CLEANUP

  # --- PRIORITY 2: Active Monitoring ---
  if has_active_trade:
    return config.STATE_ACTIVE_HUNT

  # --- PRIORITY 3: Entry Window ---
  # If we are in the window AND haven't traded yet today
  if entry_start <= now_time < entry_end:
    # Safety: Check if we already traded today to avoid double-entry
    if not _has_traded_today(cycle, env_status):
      return config.STATE_ENTRY_WINDOW
    else:
      return config.STATE_IDLE

  # --- PRIORITY 4: Pre-Game Waiting ---
  if now_time < entry_start:
    return config.STATE_WAITING

    # Default Catch-all
  return config.STATE_IDLE

def determine_cycle_state(cycle: Cycle, market_data: MarketData, env_status: EnvStatus, settings:dict=None) -> str:
  """The "Policy Manager". Checks conditions in priority order"""
  
  if cycle.last_panic_date == env_status['today']:
    return config.STATE_IDLE # Stay idle until tomorrow

  if _check_profit_target(cycle, market_data):
    return config.STATE_HARVEST_TARGET_HIT

  if _check_spread_missing(cycle, env_status, settings):
    return config.STATE_SPREAD_MISSING

  return config.STATE_IDLE
  
# --- STATE CHECK FUNCTIONS ---

def _check_profit_target(cycle: Cycle, market_data: MarketData) -> bool:
  """Rule: Spread Cost <= Target Harvest Price (50% of credit)"""
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      target = trade.target_harvest_price
      if current_cost is not None and target and current_cost <= target:
        return True
  return False

def _check_spread_missing(cycle: Cycle, env_status: EnvStatus, settings:dict=None) -> bool:

  if settings and settings.get('pause_new_entries'):
    print('not opening new spread due to: pause_new_entries')
    return False
    
  if _has_traded_today(cycle, env_status): 
    return False

  if not _is_entry_window_open(env_status, cycle.rules): 
    return False
    
  open_spreads = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]
  if len(open_spreads) > 0: 
    return False

  return True

def _has_traded_today(cycle: Cycle, env_status: EnvStatus) -> bool:
  """Checks if an INCOME trade has already occurred today."""
  today_date = env_status['today']
  eastern = pytz.timezone('US/Eastern')

  for t in cycle.trades:
    if t.role == config.ROLE_INCOME and t.entry_time:
      t_time = t.entry_time
      if t_time.tzinfo is None:
        t_time = pytz.utc.localize(t_time)

      t_date_et = t_time.astimezone(eastern).date()

      if t_date_et == today_date:
        return True
  return False

def _is_entry_window_open(env_status: dict, rules: dict) -> bool:
  """Checks only the clock-based start delay."""
  current_time = env_status['now']
  market_open_dt = dt.datetime.combine(current_time.date(), config.MARKET_OPEN_TIME)
  minutes_since_open = (current_time - market_open_dt).total_seconds() / 60.0

  return minutes_since_open >= rules.get('trade_start_delay', 15)

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
def get_scalpel_quantity(account_equity: float, debit_paid: float) -> int:
  """Calculates quantity based on 11.7% Quarter Kelly risk cap."""
  # Max dollars allowed to lose (Define Risk spread)
  # 11.7% of $50,000 = $5,850
  max_risk_dollars = account_equity * config.KELLY_QTR

  # Risk per contract is the debit paid * 100
  # e.g. $1.25 * 100 = $125
  risk_per_contract = debit_paid * 100

  if risk_per_contract <= 0: 
    return 0

  qty = int(max_risk_dollars // risk_per_contract)
  print(f'calculated qty: {qty}')
  if config.QTY_OVERIDE:
    qty = config.QTY_OVERIDE
  qty_effective = qty
  print(f'effective qty: {qty_effective}')
  return max(1, qty_effective)

def calculate_scalpel_strikes(
  chain: List[Dict], 
  rules: Dict, 
  current_price: float, 
  is_bullish: bool
) -> Optional[Dict]:
  """
  Finds the $5-wide OTM spread closest to the money that costs $1.20-$1.35.
  """
  option_type = config.TRADIER_OPTION_TYPE_CALL if is_bullish else config.TRADIER_OPTION_TYPE_PUT
  
  # 1. Filter for correct side
  side_chain = [opt for opt in chain if opt['option_type'] == option_type]
  if not side_chain: 
    return None
  

  # 2. Sort by Strike 
  # Calls: Ascending (Lowest strike first = closest to money)
  # Puts: Descending (Highest strike first = closest to money)
  side_chain.sort(key=lambda x: x['strike'], reverse=not is_bullish)

  width = float(rules.get('spread_width', 5.0))
  min_debit = float(rules.get('target_debit_min', 1.20))
  max_debit = float(rules.get('target_debit_max', 1.35))

  # Helper to get the price we actually pay (Ask on Long, Bid on Short)
  def get_debit(long_leg, short_leg):
    l_ask = float(long_leg.get('ask') or long_leg.get('last') or 0)
    s_bid = float(short_leg.get('bid') or short_leg.get('last') or 0)
    return l_ask - s_bid

  # 3. Iterate through strikes to find the pair
  for long_leg in side_chain:
    strike = long_leg['strike']

    # Must be OTM
    if is_bullish and strike <= current_price: 
      continue # Call must be above price
    if not is_bullish and strike >= current_price: 
      continue # Put must be below price

    # Find matching Short Leg ($5 wider)
    target_short_strike = (strike + width) if is_bullish else (strike - width)
    short_leg = next((opt for opt in side_chain if abs(opt['strike'] - target_short_strike) < 0.01), None)

    if not short_leg: 
      continue

    # 4. Check if the price is in our 'Scalpel' window
    debit = get_debit(long_leg, short_leg)
    if min_debit <= debit <= max_debit:
      # FOUND: This is the pair closest to the money that fits our budget
      logger.log(f"Scalpel Pair Found: {long_leg['symbol']}/{short_leg['symbol']} at ${debit:.2f} debit", 
                 level=config.LOG_INFO)
      return {
        'long_leg_data': long_leg,
        'short_leg_data': short_leg,
        'short_strike': short_leg['strike'],
        'long_strike': long_leg['strike'],
        'debit': debit,
        'is_bullish': is_bullish
      }

  return None
  
def find_closest_expiration(valid_dates: List[dt.date], target_dte: int) -> Optional[dt.date]:
  """Given a list of valid dates, finds the one closest to Today + Target DTE"""
  if not valid_dates: 
    return None
  today = dt.date.today()
  target_date = today + dt.timedelta(days=target_dte)
  return min(valid_dates, key=lambda d: abs((d - target_date).days))

def check_roll_safety(market_data: MarketData, rules: Dict) -> Tuple[bool, str]:
  """
    Validation for Roll Re-Entry.
    Bypasses Time/Frequency checks, but enforces Intraday Market Stability (Gaps).
    """
  # Intraday Drop Check
  open_price = market_data.get('open', 0)
  current_price = market_data.get('price', 0)
  if open_price > 0:
    intraday_drop_pct = (current_price - open_price) / open_price
    if intraday_drop_pct < -rules['gap_down_thresh']:
      return False, f"Intraday drop {intraday_drop_pct:.1%} - Unsafe to re-enter"

  return True, "Roll Safety Valid"

def calculate_roll_legs(
  chain: List[Dict],
  current_short_strike: float,
  width: float,
  cost_to_close: float,
  rules: Dict,
  current_price: float
) -> Optional[Dict]:
  """
    Scans for a 'Down & Out' roll. Maximizes distance.
    Finds the lowest strike that still generates enough credit to pay for 'cost_to_close'.
    """
  max_debit = float(rules.get('roll_max_debit', 0.0))
  min_dist_pct = float(rules.get('roll_min_dist_pct', 0.005))
  max_allowed_strike = current_price * (1 - min_dist_pct)
  
  # 1. Filter and Sort
  puts = [o for o in chain if o.get('option_type') == 'put']
  if not puts: 
    return None

    # Sort High to Low so we can find the "Lowest Valid" strike
  puts.sort(key=lambda x: x['strike'], reverse=True)

  best_candidate = None
  
  def get_price(opt, side):
    val = opt.get(side)
    if val is None or val == 0: 
      val = opt.get('last', 0)
    return float(val)

    # 2. Scan
  for short_candidate in puts:
    short_strike = short_candidate['strike']
    if short_strike >= current_short_strike or short_strike > max_allowed_strike: 
      continue
  
    target_long = short_strike - width
    long_candidate = next((p for p in puts if abs(p['strike'] - target_long) < 0.05), None)
    if not long_candidate: 
      continue

    # NEW MECHANICAL FIX: Ensure roots match (SPX vs SPXW)
    if long_candidate.get('root_symbol') != short_candidate.get('root_symbol'):
      continue # Skip this combination

    credit_new = get_price(short_candidate, 'bid') - get_price(long_candidate, 'ask')
    
    net_price = credit_new - cost_to_close
    if net_price >= (-max_debit):          
      # Valid candidate found. Store it.
      # We continue the loop to see if a LOWER strike (safer) also pays enough.
      best_candidate = {
        'short_leg': short_candidate,
        'long_leg': long_candidate,
        'new_credit': credit_new,
        'net_price': net_price
      }
    else:
      # Credit dropped too low. Since premiums drop as strikes drop,
      # no further candidates will work. Stop scanning.
      if best_candidate: 
        break

  return best_candidate

def check_entry_conditions(
  cycle: Cycle,
  market_data: MarketData,
  env_status: EnvStatus,
  rules: RuleSetDict
) -> Tuple[bool, str]:
  '''
  Validates if a new spread entry is allowed.
  Order: Time -> Gaps -> Short Day -> Frequency.
  '''
  current_time = env_status['now']

  # --- 1. TIME WINDOW CHECKS (Fastest fail) ---
  # A. Start Delay (e.g. 9:45 AM)
  market_open_dt = dt.datetime.combine(current_time.date(), config.MARKET_OPEN_TIME)
  minutes_since_open = (current_time - market_open_dt).total_seconds() / 60.0  
  if minutes_since_open < rules.get('trade_start_delay', 15):
    return False, "Wait time active"

  # B. Late Cutoff (e.g. 11:00 AM)
  cutoff_val = rules.get('max_entry_time') 
  cutoff_time = dt.time(11, 30) # Default
  if isinstance(cutoff_val, dt.time):
    cutoff_time = cutoff_val
  elif isinstance(cutoff_val, str):
    try:
      h, m = map(int, cutoff_val.split(':'))
      cutoff_time = dt.time(h, m)
    except ValueError: 
      pass

  cutoff_dt = dt.datetime.combine(current_time.date(), cutoff_time)
  if config.ENFORCE_LATE_OPEN_GUARDRAIL and current_time > cutoff_dt:
    return False, f"Time {current_time.strftime('%H:%M')} past cutoff {cutoff_time.strftime('%H:%M')}"

  # --- 2. MARKET GAP CHECKS (Fast Math) ---
  open_price = market_data.get('open', 0)
  prev_close = market_data.get('previous_close', 0)
  current_price = market_data.get('price', 0)
  gap_thresh = rules.get('gap_down_thresh', 0.01) # Default 1%

  # Overnight Gap
  if prev_close > 0:
    overnight_drop_pct = (open_price - prev_close) / prev_close
    print(f'overnight  drop %: {overnight_drop_pct}')
    if overnight_drop_pct < -gap_thresh:
      return False, f"Overnight gap {overnight_drop_pct:.1%} exceeds limit"

  # Intraday Crash
  if open_price > 0:
    intraday_drop_pct = (current_price - open_price) / open_price
    if intraday_drop_pct < -gap_thresh:
      return False, f"Intraday drop {intraday_drop_pct:.1%} exceeds limit"

  # --- 3. SHORT DAY CHECK (Logic) ---
  # Tradier 'next_state_change' is usually "HH:MM" (24h)
  # Standard close is 16:00. Early close is usually 13:00.
  next_change_str = env_status.get('next_state_change', '16:00')
  try:
    # Parse hour from string "13:00"
    close_hour = int(next_change_str.split(':')[0])

    # If market closes before 15:00 (3 PM), it's a Short Day.
    if close_hour < 15:
      return False, f"Market closes early ({next_change_str}) - Entry Blocked"
  except (ValueError, IndexError):
    # If format is weird, assume standard day and proceed, or log warning
    pass

  return True, "Entry valid"


def calculate_spread_strikes(
  chain: List[Dict],
  rules: Dict,
  option_type: str = config.TRADIER_OPTION_TYPE_PUT
) -> Optional[Tuple[float, float]]:
  """
  Price-First Selection Algorithm.
  1. Finds all spreads with valid Width.
  2. Filters for Credit between Min/Max rules.
  3. Selects the SAFEST (Lowest Strike) candidate that gets paid.
  """
  side_chain = [opt for opt in chain if opt['option_type'] == option_type]
  if not side_chain: 
    return None
  
  # Helper to get price (Midpoint fallback to Last for Sandbox safety)
  def get_prices(opt):
    bid = float(opt.get('bid', 0) or 0)
    ask = float(opt.get('ask', 0) or 0)
    return bid, ask

  def to_nickel(val):
    """Rounds to nearest 0.05"""
    return round(val * 20) / 20.0

  spread_width = rules['spread_width']
  min_credit = rules['spread_min_premium']
  max_credit = rules['spread_max_premium']

  valid_candidates = []
  # DEBUG COUNTERS
  reject_liquidity = 0
  reject_price_low = 0
  reject_price_high = 0
  # 1. Scan all potential short legs
  for short_leg in side_chain:
    s_bid, s_ask = get_prices(short_leg)
    if s_bid == 0 or s_ask == 0: 
      continue

    # SPX rule of thumb: If bid/ask spread > 0.75, it's not a real quote
    liquidity_threshold = rules.get('max_bid_ask_spread', config.MAX_BID_ASK_SPREAD)
    if (s_ask - s_bid) > liquidity_threshold: 
      reject_liquidity += 1
      continue

    short_strike = short_leg['strike']

    # Find matching Long Leg
    if option_type == config.TRADIER_OPTION_TYPE_PUT:
      long_strike = short_strike - spread_width
    else:
      long_strike = short_strike + spread_width

      # Exact match check
    long_leg = next((opt for opt in side_chain if abs(opt['strike'] - long_strike) < 0.01), None)
    if not long_leg: 
      continue

    # 2. Check Price (The "Money Talks" Filter)
    l_bid, l_ask = get_prices(long_leg)
    if l_bid == 0 or l_ask == 0: 
      continue
    if (l_ask - l_bid) > liquidity_threshold: 
      reject_liquidity += 1
      continue
    
    # 2. Midpoint Calc
    s_mid = (s_bid + s_ask) / 2.0
    l_mid = (l_bid + l_ask) / 2.0
    raw_credit = s_mid - l_mid
    credit = to_nickel(raw_credit)

    # Does it pay the rent?
    if credit < min_credit:
      reject_price_low += 1
    elif credit > max_credit:
      reject_price_high += 1
    else:
      valid_candidates.append({
        'short_strike': short_strike,
        'long_strike': long_strike,
        'credit': credit
      })

  if not valid_candidates:
    # DEBUG PRINT
    print(f"DEBUG REJECT: Scanned {len(side_chain)} legs.")
    print(f"   Rejected Liquidity (>1.50 wide): {reject_liquidity}")
    print(f"   Rejected Low Price (<{min_credit}): {reject_price_low}")
    print(f"   Rejected High Price (>{max_credit}): {reject_price_high}")
    return None

  # 3. Pick the Winner
  # Strategy: "Maximize Distance". 
  # Sort by Short Strike Ascending (Lowest first).
  # The first item is the furthest OTM strike that meets our income requirement.
  valid_candidates.sort(key=lambda x: x['short_strike'])

  best = valid_candidates[0]

  return best['short_strike'], best['long_strike']
  
def validate_premium_and_size(
  short_leg: Dict,
  long_leg: Dict,
  rules: RuleSetDict
) -> Tuple[bool, float, str]:

  # FIX: Remove 'last' fallback. Use 0.0 if bid/ask missing.
  s_bid = float(short_leg.get('bid', 0) or 0)
  s_ask = float(short_leg.get('ask', 0) or 0)

  l_bid = float(long_leg.get('bid', 0) or 0)
  l_ask = float(long_leg.get('ask', 0) or 0)

  # Safety check
  if s_bid == 0 or l_ask == 0:
    return False, 0.0, "Illiquid strikes (Bid/Ask missing)"

  #s_mid = (s_bid + s_ask) / 2.0
  #l_mid = (l_bid + l_ask) / 2.0

  #net_credit = s_mid - l_mid
  s_mid = (s_bid + s_ask) / 2.0
  l_mid = (l_bid + l_ask) / 2.0
  raw_credit = s_mid - l_mid
  mid_credit = round(raw_credit * 20) / 20

  if mid_credit < rules['spread_min_premium']:
    return False, mid_credit, f"Credit {mid_credit:.2f} below min {rules['spread_min_premium']}"

  if mid_credit > rules['spread_max_premium']:
    return False, mid_credit, f"Credit {mid_credit:.2f} exceeds max {rules['spread_max_premium']}"

  return True, mid_credit, "Premium Valid"

def get_spread_quantity(
  hedge_quantity: int,
  spread_price: float,
  rules: Dict
) -> int:
  """Calculates position size using '5/C' rule (or scaled equivalent)"""
  if spread_price <= 0: 
    return 0

  raw_qty = int(round(hedge_quantity * rules['spread_size_factor'] / spread_price))

  # Apply Safety Cap
  capped_qty = min(raw_qty, hedge_quantity * config.MAX_SPREAD_TO_HEDGE_RATIO)
  return max(1, capped_qty)

def evaluate_entry(
  cycle: Cycle,
  chain: List[Dict],
  market_data: MarketData,
  env_status: Dict,
  rules: Dict
) -> Tuple[bool, Dict, str]:
  """
  Master entry function.
  Orchestrates: Conditions -> Strikes -> Validation -> Sizing.
  """

  # 1. Broad Market Checks
  is_valid_env, env_reason = check_entry_conditions(
    cycle,
    market_data,
    env_status,
    rules
    )
  if not is_valid_env:
      return False, {}, env_reason

  # 2. Prerequisites
  hedge = cycle.hedge_trade_link
  if not hedge or hedge.status != config.STATUS_OPEN:
    return False, {}, "No active (OPEN) hedge linked to cycle"

  # 3. Strike Selection
  strikes = calculate_spread_strikes(
    chain,
    rules=rules 
  )
  if not strikes:
      return False, {}, "No spreads found that match Target Credit (Min/Max)"

  short_strike, long_strike = strikes

  # 4. Data Extraction
  short_leg = next(leg for leg in chain if leg['strike'] == short_strike and leg['option_type'] == 'put')
  long_leg = next(leg for leg in chain if leg['strike'] == long_strike and leg['option_type'] == 'put')

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

def get_zombie_trades(cycle: Cycle, positions: List[Dict]) -> List[Trade]:
  """
    Finds trades that are OPEN in DB but MISSING from Broker positions.
    Assumes missing means 'Expired' or 'Closed externally'.
    """
  zombies = []
  broker_symbols = set()
  for p in positions:
    sym = p.get('symbol')
    if sym:
      broker_symbols.add(sym)

    # 2. Check each Open Trade
  for trade in cycle.trades:
    if trade.status == config.STATUS_OPEN:
      legs = getattr(trade, 'legs', [])
      if not legs: 
        zombies.append(trade)
        continue

        # Check if ANY of the trade's legs exist in the broker
        # Logic: If the broker has dropped ALL legs of this trade, it is expired/closed.
      is_active_on_broker = False
      for leg in legs:
        if leg.occ_symbol in broker_symbols:
          is_active_on_broker = True
          break

      if not is_active_on_broker:
        # Double check dates? 
        # If today > entry_date, and it's missing, it definitely expired/closed.
        # If today == entry_date, maybe latency? 
        # For safety in this strategy, "Missing from Broker" = "Closed".
        zombies.append(trade)
        
  print(f'zombies: {zombies}')
  return zombies

# timezone helper
def _is_today(dt_val, today_date):
  """Checks if a DB timestamp (UTC) happened 'Today' (Eastern)."""
  if not dt_val: 
    return False
  if dt_val.tzinfo is None:
    dt_val = pytz.utc.localize(dt_val)

  eastern = pytz.timezone('US/Eastern')
  return dt_val.astimezone(eastern).date() == today_date

