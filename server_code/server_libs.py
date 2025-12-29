from shared import config
from shared import types
from shared.classes import Cycle, Trade

import datetime as dt
from typing import Optional

def can_run_automation(env, cycle):
  # STUB: Always say yes for testing
  return True

def is_db_consistent(cycle, positions):
  # STUB: Assume DB is perfect
  return True

def determine_cycle_state(cycle, market_data: types.MarketData)->str:
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

# --- ATOMIC PREDICATE FUNCTIONS ---

def _check_panic_harvest(cycle, market_data):
  """
  Rule: If Net Unit PnL (Hedge Daily Gain + Spread PnL) > $350 per unit.
  """
  # STUB
  # Calculation: (current_hedge_mark - cycle.daily_hedge_ref) + spread_pl
  return False

def _check_roll_needed(cycle, market_data):
  """
  Rule: If Spread Ask Price > 3.0 * Initial Credit.
  """
  # STUB
  # Logic: current_spread_ask >= cycle.spread_trades[0].roll_trigger
  return False

def _check_hedge_maintenance(cycle, market_data):
  """
  Rule: If Hedge Delta < 15 or > 40, OR DTE < 60.
  """
  # STUB
  if not cycle.hedge_trade_link:
    return False
  # Logic: Check delta/dte from market_data['hedge_greeks']
  return False

def _check_profit_target(cycle, market_data):
  """
  Rule: If Spread Profit >= 50% of max profit.
  """
  # STUB
  return False

def _check_hedge_missing(cycle):
  """
  Rule: Cycle is OPEN but has no active Hedge Trade linked.
  """
  return cycle.hedge_trade_link is None

def _check_spread_missing(cycle):
  """
  Rule: Hedge exists, but no active Income Trade (Spread) exists.
  """
  # We check if there are any active trades tagged as 'INCOME'
  # Assuming cycle.spread_trades is populated by the Cycle class
  return len(cycle.spread_trades) == 0

def alert_human(message, level=config.ALERT_INFO):
  print(f"ALERT [{level}]: {message}")

# ... Add other stubs (select_hedge_strike, calculate_roll_legs) as we hit them

def get_target_hedge_date(cycle: Cycle, current_date:Optional[dt.date]=None)->dt.date:
  """
    Calculates the target expiration date based on the Cycle's RuleSet.
  """
  if not current_date:
    current_date = dt.datetime.now().date()
    target_days = cycle.rules.get('target_hedge_dte', 90)

  return current_date + dt.timedelta(days=target_days)