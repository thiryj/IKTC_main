import anvil.server
import anvil.tables as tables
from anvil.tables import app_tables
import datetime as dt
import pytz

from shared import config
from . import server_db
from . import server_api
from . import server_libs

# timezone helper
def _is_today(dt_val, today_date):
  """Checks if a DB timestamp (UTC) happened 'Today' (Eastern)."""
  if not dt_val: return False
  if dt_val.tzinfo is None:
    dt_val = pytz.utc.localize(dt_val)

  eastern = pytz.timezone('US/Eastern')
  return dt_val.astimezone(eastern).date() == today_date

# --- DASHBOARD DATA ---
@anvil.server.callable
def get_dashboard_state():
  """Fetches all data required to render the Dashboard UI"""
  # 1. Global Settings & Env
  settings = app_tables.settings.get()
  env_status = server_api.get_environment_status()

  # 2. Active Cycle
  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)

  # Defaults
  data = {
    'active_env': config.ACTIVE_ENV,
    'automation_enabled': settings['automation_enabled'] if settings else False,
    'market_status': env_status.get('status', 'CLOSED'),
    'market_time': env_status.get('now'),
    'cycle_active': False,
    'net_daily_pnl': 0.0,
    # Components
    'hedge': {'active': False, 'status_color': 'gray', 'symbol': 'No Hedge', 'details': '-'},
    'spread': {'active': False, 'status_color': 'gray', 'symbol': 'No Spread', 'details': '-'},
    'log_summary': [], # Can populate later
    'closed_session': {
    'visible': False,
    'pnl': 0.0,
    'text': "",
    'color': "gray"
    }
  }
  if not cycle: return data

  # 3. Hydrate Cycle Data
  data['cycle_active'] = True
  data['cycle_id'] = cycle.id

  # Fetch Live Data (Price & Greeks)
  # We use the existing snapshot logic to avoid code duplication
  market_data = server_api.get_market_data_snapshot(cycle)
  current_state = server_libs.determine_cycle_state(cycle, market_data)
  data['decision_state'] = current_state

  # --- HEDGE COMPONENT ---
  hedge = cycle.hedge_trade_link
  hedge_pnl_day = 0.0
  if hedge and hedge.status == config.STATUS_OPEN:
    # PnL Calc
    current_price = market_data.get('hedge_last', 0.0)
    ref_price = cycle.daily_hedge_ref or 0.0
    # If ref is 0, use entry price as fallback
    if ref_price == 0: ref_price = hedge.entry_price or 0.0

    hedge_pnl_day = (current_price - ref_price) * config.DEFAULT_MULTIPLIER * hedge.quantity

    # Health Check (Logic from server_libs)
    # Check maintenance flags (Delta/DTE)
    needs_maint = server_libs._check_hedge_maintenance(cycle, market_data)

    status_color = "green"
    status_text = f"{hedge.quantity}x {hedge.legs[0].occ_symbol if hedge.legs else 'Hedge'}"
    sub_text = f"Day PnL: ${hedge_pnl_day:+.2f} (Ref: {ref_price:.2f})"
    if needs_maint:
      status_color = "#FFC107" # Amber/Yellow
      status_text += " (Roll Needed)"

    data['hedge'] = {
      'active': True,
      'status_color': status_color,
      'symbol': status_text,
      'details': sub_text,
      'pnl': hedge_pnl_day,
      'price': current_price
    }

    # --- SPREAD COMPONENT ---
  spread_pnl_total = 0.0
  closed_spreads = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_CLOSED]
  today_date = env_status['today']

  for t in closed_spreads:
    # Check if it closed today
    if _is_today(t.exit_time, today_date):
      # DB stores PnL per share. Multiply by Qty * 100.
      pnl_dollars = (t.pnl or 0.0) * config.DEFAULT_MULTIPLIER * t.quantity
      spread_pnl_total += pnl_dollars
    
    
  active_spreads = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]
  # Default display values (if no open trade)
  spread_status_text = "No Active Spread"
  spread_status_color = "gray"
  spread_details = f"Realized Today: ${spread_pnl_total:+.2f}"
  if active_spreads:
    # Assuming single spread for display, or sum if multiple
    trade = active_spreads[0] 

    # Get Marks
    current_debit = market_data.get('spread_marks', {}).get(trade.id, 0.0)
    entry_credit = trade.entry_price or 0.0

    # PnL: (Credit - Debit) * 100 * Qty
    unrealized  = (entry_credit - current_debit) * config.DEFAULT_MULTIPLIER * trade.quantity
    spread_pnl_total += unrealized

    # Status Checks
    roll_trigger = trade.roll_trigger_price or 999.0
    harvest_target = trade.target_harvest_price or 0.0

    spread_status_color = "green" if unrealized >= 0 else "red"

    # Alarm Conditions
    is_threatened = (current_debit >= roll_trigger)
    is_winning = (current_debit <= harvest_target)

    spread_status_text = f"{trade.quantity}x Spread"
    if is_threatened:
      spread_status_color = "#FF0000" # Bright Red / Flash logic in UI
      spread_status_text += " (ROLL TRIGGER)"
    elif is_winning:
      spread_status_color = "#00CC00" # Bright Green
      spread_status_text += " (Harvesting...)"

    spread_details = f"Open: ${unrealized:+.2f} | Closed: ${spread_pnl_total - unrealized:+.2f} | Mark: {current_debit:.2f}"

    data['spread'] = {
      'active': True,
      'status_color': spread_status_color,
      'symbol': spread_status_text,
      'details': spread_details,
      'pnl': spread_pnl_total
    }

  # --- CLOSED SPREAD ---
  today_date = env_status['today']
  closed_today = [
    t for t in cycle.trades 
    if t.role == config.ROLE_INCOME 
    and t.status == config.STATUS_CLOSED
    and _is_today(t.exit_time, today_date) # Uses the helper we added
  ]

  realized_pnl = 0.0
  if closed_today:
    trade_count = len(closed_today)
  
    for t in closed_today:
      # PnL per share * Qty * Multiplier
      realized_pnl += (t.pnl or 0.0) * config.DEFAULT_MULTIPLIER * t.quantity
  
      # Formatting
    color = "green" if realized_pnl >= 0 else "red"
    # e.g., "2x Spreads Closed"
    summary = f"{trade_count}x Spread{'s' if trade_count > 1 else ''} Closed"
  
    data['closed_session'] = {
      'visible': True,
      'pnl': realized_pnl,
      'text': summary,
      'color': color
    }
  
  # --- AGGREGATE ---
  data['net_daily_pnl'] = hedge_pnl_day + spread_pnl_total

  return data

# --- ACTIONS ---

@anvil.server.callable
def toggle_automation_status(enabled: bool):
  """Called by the UI Switch"""
  # Assuming 'settings' table has 1 row.
  row = app_tables.settings.get()
  if row:
    row['automation_enabled'] = enabled
    # Log it
    from . import server_logging as logger
    logger.log(f"User toggled automation to: {enabled}", level=config.LOG_INFO, source=config.LOG_SOURCE_CLIENT)
  return enabled

@anvil.server.callable
def get_log_stream(level_filter=None, limit=50):
  """Returns latest logs for the Data Grid"""
  
  # Return the iterator directly (Anvil handles pagination)
  return app_tables.logs.search(
    tables.order_by("timestamp", ascending=False),
    environment=config.ACTIVE_ENV
  )
