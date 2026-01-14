import anvil.email
from shared import config
from shared.classes import Cycle, Trade
from shared.types import MarketData, EnvStatus, RuleSetDict
from typing import Optional, Tuple, Dict, List
import datetime as dt
import pytz

from . import server_logging as logger

# --- ORCHESTRATION HELPERS ---

def can_run_automation(env_status: dict, settings:dict) -> bool:
  """Checks if the bot is allowed to run based on Market Status and Settings"""
  if not settings or not settings.get('automation_enabled'): 
    return False
    
  if config.ENFORCE_TRADING_HOURS and env_status.get('status') != 'OPEN':
      return False

  return True

def is_db_consistent(cycle: Optional[Cycle], positions: List[dict]) -> bool:
  # TODO: Implement reconciliation logic.
  # Compare cycle.trades vs Tradier positions. Return False if mismatch found.
  return True

def determine_cycle_state(cycle: Cycle, market_data: MarketData, env_status: EnvStatus) -> str:
  """The "Policy Manager". Checks conditions in priority order"""
  
  if _check_panic_harvest(cycle, market_data):
    return config.STATE_PANIC_HARVEST

  if _check_roll_needed(cycle, market_data):
    return config.STATE_ROLL_REQUIRED

  if _check_hedge_maintenance(cycle, market_data):
    return config.STATE_HEDGE_ADJUSTMENT_NEEDED

  if _check_profit_target(cycle, market_data):
    return config.STATE_HARVEST_TARGET_HIT

  if _check_hedge_missing(cycle):
    return config.STATE_HEDGE_MISSING

  if _check_spread_missing(cycle, env_status):
    return config.STATE_SPREAD_MISSING

  return config.STATE_IDLE

# --- STATE CHECK FUNCTIONS ---

def _check_panic_harvest(cycle: Cycle, market_data: MarketData) -> bool:
  """Rule: Net Unit PnL (Hedge Gain + Spread PnL) > Panic Threshold"""
  hedge = cycle.hedge_trade_link
  
  if not hedge:
    return False

  if hedge.status != config.STATUS_OPEN:
    return False
    
  # 1. Calculate Hedge PnL (Daily Change)
  # Logic: (Current Price - Daily Reference) * Multiplier * Qty
  hedge_ref = cycle.daily_hedge_ref or 0.0
  if hedge_ref == 0: 
    return False 

  hedge_current = market_data.get('hedge_last', 0.0)
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
  threshold = cycle.rules['panic_threshold_dpu'] * cycle.hedge_trade_link.quantity
  if net_unit_pnl > threshold:
    logger.log(f"Panic Threshold Breached: ${net_unit_pnl:.2f} > ${threshold:.2f} (Hedge: {hedge_pnl:.2f}, Spread: {spread_pnl:.2f})", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_LIBS)
    return True

  return False

def _check_roll_needed(cycle: Cycle, market_data: MarketData) -> bool:
  """Rule: Spread Ask Price > Roll Trigger Price (3x Credit)"""
  marks = market_data.get('spread_marks', {})

  for trade in cycle.trades:
    if trade.role == config.ROLE_INCOME and trade.status == config.STATUS_OPEN:
      current_cost = marks.get(trade.id)
      trigger = trade.roll_trigger_price
      if current_cost and trigger and current_cost >= trigger:
        logger.log(f"Roll Triggered for Trade {trade.id}: Cost {current_cost} >= Trigger {trigger}", 
                   level=config.LOG_INFO, source=config.LOG_SOURCE_LIBS)
        return True
  return False

def _check_hedge_maintenance(cycle: Cycle, market_data: MarketData) -> bool:
  """Rule: If Hedge Delta is outside of guardrails, OR DTE < 60"""
  if not cycle.hedge_trade_link:
    return False
    
  if cycle.hedge_trade_link.status != config.STATUS_OPEN:
    return False
    
  # 1. Check Time (DTE)
  current_dte = market_data.get('hedge_dte', 999)
  min_dte = cycle.rules.get('hedge_min_dte', 60)
  if current_dte < min_dte:
    logger.log(f"Hedge Maintenance: DTE {current_dte} < Limit {min_dte}", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_LIBS)
    return True

  # 2. Check Delta
  # Puts have negative delta, use ABS
  # Handle Sandbox/Data Glitch where delta is exactly 0.0
  raw_delta = market_data.get('hedge_delta')
  # FIX: If delta is 0 or None, assume data is stale and DO NOT roll.
  if not raw_delta: 
    return False
  current_delta = abs(raw_delta)
  min_delta = cycle.rules.get('hedge_min_delta', 0.15)
  max_delta = cycle.rules.get('hedge_max_delta', 0.40)
  if current_delta < min_delta or current_delta > max_delta:
    logger.log(f"Hedge Maintenance: Delta {current_delta} out of bounds ({min_delta}-{max_delta})",
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_LIBS)
    return True

  return False

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

def _check_hedge_missing(cycle: Cycle) -> bool:
  """Rule: Cycle is OPEN but has no *ACTIVE* Hedge Trade linked"""
  # 1. No link at all? Missing.
  if not cycle.hedge_trade_link: return True

  # 2. Link exists, but status is CLOSED? Missing. (THE CRITICAL FIX)
  if cycle.hedge_trade_link.status != config.STATUS_OPEN:  return True

  return False

def _check_spread_missing(cycle: Cycle, env_status: EnvStatus) -> bool:
  if _has_traded_today(cycle, env_status):  #only one spread per day
    return False
    
  open_spreads = [
    t for t in cycle.trades 
    if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN
  ]
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
  if not valid_dates: return None
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
    if short_strike >= current_short_strike: continue

    target_long = short_strike - width
    long_candidate = next((p for p in puts if abs(p['strike'] - target_long) < 0.05), None)
    if not long_candidate: continue

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
    except: pass

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

  # --- 4. FREQUENCY CHECK (only one spread open per day) ---
  if config.ENFORCE_FREQUENCY_CHECKS and _has_traded_today(cycle, env_status):
    return False, "Daily limit reached (1 spread per day)"

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
    
  valid_options = [o for o in side_chain if get_delta(o) > 0.01]
  if not valid_options: return None # No valid data found in chain

  short_leg = min(side_chain, key=lambda x: abs(get_delta(x) - target_delta))
  found_delta = get_delta(short_leg)
  if abs(found_delta - target_delta) > config.MAX_DELTA_ERROR:
    logger.log(f"Best strike {short_leg['strike']} (Delta {found_delta}) is too far from target {target_delta}. Skipping.",
              level=config.LOG_INFO,
              source=config.LOG_SOURCE_ORCHESTRATOR
              )    
    return None
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
  rules: RuleSetDict
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

  return zombies