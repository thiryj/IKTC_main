import anvil.server
import anvil.tables as tables
from anvil.tables import app_tables
import datetime as dt

from shared import config
from . import server_db
from . import server_api
from . import server_libs

# --- DASHBOARD DATA ---

@anvil.server.callable
def get_dashboard_state():
  """
    Fetches all data required to render the Dashboard UI.
    """
  # 1. Global Settings & Env
  settings = app_tables.settings.get()
  env_status = server_api.get_environment_status()

  # 2. Active Cycle
  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)

  # Defaults
  data = {
    'automation_enabled': settings['automation_enabled'] if settings else False,
    'market_status': env_status.get('status', 'CLOSED'),
    'market_time': env_status.get('now'),
    'cycle_active': False,
    'net_daily_pnl': 0.0,
    # Components
    'hedge': {'active': False, 'status_color': 'gray', 'text': 'No Hedge'},
    'spread': {'active': False, 'status_color': 'gray', 'text': 'No Spread'},
    'log_summary': [] # Can populate later
  }

  if not cycle:
    return data

    # 3. Hydrate Cycle Data
  data['cycle_active'] = True
  data['cycle_id'] = cycle.id

  # Fetch Live Data (Price & Greeks)
  # We use the existing snapshot logic to avoid code duplication
  market_data = server_api.get_market_data_snapshot(cycle)

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
  active_spreads = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]

  if active_spreads:
    # Assuming single spread for display, or sum if multiple
    trade = active_spreads[0] 

    # Get Marks
    current_debit = market_data.get('spread_marks', {}).get(trade.id, 0.0)
    entry_credit = trade.entry_price or 0.0

    # PnL: (Credit - Debit) * 100 * Qty
    trade_pnl = (entry_credit - current_debit) * config.DEFAULT_MULTIPLIER * trade.quantity
    spread_pnl_total += trade_pnl

    # Status Checks
    roll_trigger = trade.roll_trigger_price or 999.0
    harvest_target = trade.target_harvest_price or 0.0

    status_color = "green" # Default winning
    if trade_pnl < 0: status_color = "red"

      # Alarm Conditions
    is_threatened = (current_debit >= roll_trigger)
    is_winning = (current_debit <= harvest_target)

    status_text = f"{trade.quantity}x Spread"
    if is_threatened:
      status_color = "#FF0000" # Bright Red / Flash logic in UI
      status_text += " (ROLL TRIGGER)"
    elif is_winning:
      status_color = "#00CC00" # Bright Green
      status_text += " (Harvesting...)"

    data['spread'] = {
      'active': True,
      'status_color': status_color,
      'symbol': status_text,
      'details': f"Open PnL: ${trade_pnl:+.2f} | Mark: {current_debit:.2f} (Trig: {roll_trigger:.2f})",
      'pnl': trade_pnl
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
