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
  settings_row = app_tables.settings.get()
  settings = dict(settings_row) if settings_row else {}
  env_status = server_api.get_environment_status()
  
  # 2. Active Cycle
  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
  if not cycle:
    return {
      'active_env': config.ACTIVE_ENV,
      'automation_enabled': settings.get('automation_enabled', False),
      'market_status': env_status.get('status', 'CLOSED'),
      'market_time': env_status.get('now'),
      'cycle_active': False, # UI uses this to hide cards
      'net_daily_pnl': 0.0,
      'bot_status_text': "IDLE (NO CYCLE)",
      'bot_status_color': "gray",
      'hedge': {
        'active': False, 
        'symbol': 'No Active Hedge', 
        'status_color': 'gray',
        'details': '-'
      },
      'spread': {
        'active': False, 
        'symbol': 'No Active Spread', 
        'status_color': 'gray',
        'details': '-'
      },
      'closed_session': {
        'visible': False, 
        'pnl': 0.0,
        'text': '', 
        'color': 'gray'
      }
    }
  market_data = server_api.get_market_data_snapshot(cycle)
  status_meta = _get_bot_status_metadata(settings, env_status, cycle, market_data)

  last_hb = settings['last_bot_heartbeat']
  is_stale = False
  if last_hb and settings['automation_enabled']:
    # Ensure we are comparing UTC to UTC if needed, 
    now_aware = dt.datetime.now(dt.timezone.utc) 
    diff = (now_aware - last_hb).total_seconds()
    is_stale = diff > config.UI_REFRESH_SECONDS * 2 # 5 minutes

  # Defaults
  data = {
    'last_heartbeat': last_hb,
    'bot_is_stale': is_stale,
    'active_env': config.ACTIVE_ENV,
    'automation_enabled': settings['automation_enabled'] if settings else False,
    'market_status': env_status.get('status', 'CLOSED'),
    'market_time': env_status.get('now'),
    'cycle_active': False,
    'net_daily_pnl': 0.0,
    # Status
    'bot_status_text': status_meta['text'],
    'bot_status_color': status_meta['color'],
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
  
  # --- HEDGE COMPONENT ---
  hedge = cycle.hedge_trade_link
  hedge_pnl_day = 0.0
  if hedge and hedge.status == config.STATUS_OPEN:
    # PnL Calc
    current_price = market_data.get('hedge_last', 0.0)
    ref_price = cycle.daily_hedge_ref or 0.0
    if ref_price == 0: ref_price = hedge.entry_price or 0.0

    hedge_pnl_day = (current_price - ref_price) * config.DEFAULT_MULTIPLIER * hedge.quantity

    needs_maint = server_libs._check_hedge_maintenance(cycle, market_data)

    current_delta = market_data.get('hedge_delta', 0.0)
    current_dte = market_data.get('hedge_dte', 0)
    status_color = "green"
    status_text = f"{hedge.quantity}x {hedge.legs[0].occ_symbol if hedge.legs else 'Hedge'}"
    sub_text = f"PnL: ${hedge_pnl_day:+.0f} | Delta: {current_delta:.2f} | {current_dte} DTE"
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
    def safe_float(val, default=0.0) -> float:
      try:
        if val is None: return default
        return float(val)
      except (ValueError, TypeError):
        return default
        
    gauge_data = None
    if active_spreads:
      trade = active_spreads[0]
      # We want to visualize the DEBIT (Cost to Close)
      # Entry (Max Risk) -> Trigger (Panic) -> Entry (Breakeven) -> Target (Win) -> 0 (Max Win)
      entry_px = trade.entry_price if trade.entry_price is not None else 0.0
      gauge_data = {
        'current': safe_float(market_data.get('spread_marks', {}).get(trade.id, 0.0)),
        'entry': entry_px,
        'target': trade.target_harvest_price if trade.target_harvest_price is not None else (entry_px * 0.5),
        'trigger': trade.roll_trigger_price if trade.roll_trigger_price is not None else (entry_px * 3.0),
        'max_loss': trade.roll_trigger_price * 1.5 # Just for scaling the chart axis
      }
      if gauge_data['trigger'] == 0: gauge_data['trigger'] = 1.0
    data['spread_gauge'] = gauge_data

  # --- CLOSED SPREAD ---
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
      realized_pnl += (t.pnl or 0.0) * config.DEFAULT_MULTIPLIER * t.quantity
  
    color = "green" if realized_pnl >= 0 else "red"
    summary = f"{trade_count}x Spread{'s' if trade_count > 1 else ''} Closed"
  
    data['closed_session'] = {
      'visible': True,
      'pnl': realized_pnl,
      'text': summary,
      'color': color
    }
  
  # --- AGGREGATE ---
  data['net_daily_pnl'] = round(hedge_pnl_day + spread_pnl_total,2)

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

# ---Private helpers ---
def _get_bot_status_metadata(settings, env_status, cycle, market_data):
  """Determines the text and color for the main status indicator"""
  # 1. Check Global Switch
  if not settings.get('automation_enabled'):
    return {'text': "DISABLED", 'color': "#FF0000"}

    # 2. Check Market Hours
  if env_status.get('status') != 'OPEN':
    return {'text': "SLEEPING (MARKET CLOSED)", 'color': "gray"}

    # 3. Check Cycle Existence
  if not cycle:
    return {'text': "NO CYCLE", 'color': "#FFC107"} # Amber

    # 4. Check Logic State
  current_state = server_libs.determine_cycle_state(cycle, market_data, env_status)

  if current_state == config.STATE_IDLE:
    open_spreads = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]
    if open_spreads:
      return {'text': "MONITORING", 'color': "blue"}
      
    # we are flat.  did we already trade (open then close)?
    if server_libs._has_traded_today(cycle, env_status):
      return {'text': "DONE FOR DAY", 'color': "#00CC00"} # Bright Green
    else:
      return {'text': "WATCHING (IDLE)", 'color': "green"}
  else:
    # Active State Coloring
    text = current_state
    color = "blue"

    if "PANIC" in current_state or "ROLL" in current_state:
      color = "#FF0000" # Red (Action)
    elif "HARVEST" in current_state:
      color = "#00CC00" # Green (Profit)

    return {'text': text, 'color': color}

@anvil.server.callable
def get_trades_crud_list() -> list:
  """Returns a list of dictionaries for the CRUD data grid."""
  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
  if not cycle: return []

  # Return data formatted for the UI
  return [
    {
      'id': t.id,
      'role': t.role,
      'status': t.status,
      'qty': t.quantity,
      'entry': t.entry_price,
      'harvest': t.target_harvest_price or 0.0,
      'trigger': t.roll_trigger_price or 0.0,
      'symbol': t.legs[0].occ_symbol if t.legs else "No Legs"
    } for t in cycle.trades
  ]

@anvil.server.callable
def delete_trade_manual(trade_id: str) -> bool:
  """Manual surgery to remove a trade."""
  return server_db.crud_delete_trade(trade_id)

#-----------------------------------------#
# KPI Dashboard
@anvil.server.callable
def get_performance_dashboard_stats() -> dict:
  """Aggregates all Strategy KPIs for the Stats Page."""
  # 1. Fetch all closed cycles for the active environment
  cycles = list(app_tables.cycles.search(
    status=config.STATUS_CLOSED, 
    account=config.ACTIVE_ENV
  ))

  total_cycles = len(cycles)
  if total_cycles == 0: return {'count': 0}

  # 2. Headline Stats
  total_net_pnl = sum([float(c['total_pnl'] or 0) for c in cycles])
  winning_cycles_count = len([c for c in cycles if (c['total_pnl'] or 0) > 0])

  # 3. Tactical Analysis (Iterate through cycles)
  rolls_triggered = 0
  rolls_saved_cycle = 0 # Rolled and ended profitable
  panics = 0
  windfalls = 0

  for c in cycles:
    # A. Roll Check: Did this cycle have more than one income spread?
    income_trades = app_tables.trades.search(cycle=c, role=config.ROLE_INCOME)
    if len(list(income_trades)) > 1:
      rolls_triggered += 1
      if (c['total_pnl'] or 0) > 0:
        rolls_saved_cycle += 1

    # B. Exit Analysis: Check notes for Panic/Windfall markers
    notes = (c['notes'] or "").upper()
    if "PANIC" in notes: panics += 1
    if "WINDFALL" in notes: windfalls += 1

  # 4. Income Trade Efficiency
  all_closed_income = list(app_tables.trades.search(
    role=config.ROLE_INCOME, 
    status=config.STATUS_CLOSED,
    cycle=anvil.tables.query.any_of(*cycles)
  ))

  income_wins = [t for t in all_closed_income if (t['pnl'] or 0) > 0]
  avg_win_amt = (sum([t['pnl'] or 0 for t in income_wins]) / len(income_wins)) * 100 if income_wins else 0

  return {
    'total_cycles': total_cycles,
    'total_net_pnl': round(total_net_pnl, 2),
    'win_rate': round((winning_cycles_count / total_cycles) * 100, 1),
    'avg_win_dollars': round(avg_win_amt, 2),

    # Tactical KPIs
    'roll_trigger_rate': round((rolls_triggered / total_cycles) * 100, 1),
    'roll_success_rate': round((rolls_saved_cycle / rolls_triggered * 100), 1) if rolls_triggered > 0 else 0,
    'panic_rate': round((panics / total_cycles) * 100, 1),
    'windfall_rate': round((windfalls / total_cycles) * 100, 1),

    # Average Profit per Cycle
    'avg_cycle_pnl': round(total_net_pnl / total_cycles, 2)
  }

@anvil.server.callable
def get_performance_headlines() -> dict:
  """Calculates the Top-Row Business Metrics for the Stats Page."""
  settings = app_tables.settings.get()
  account_equity = float(settings['total_account_equity'] or 40000)

  # 1. Fetch All Closed Cycles
  cycles = list(app_tables.cycles.search(status=config.STATUS_CLOSED, account=config.ACTIVE_ENV))
  if not cycles:
    return {'active': False}

  # 2. Time Calculations
  first_date = min([c['start_date'] for c in cycles])
  days_active = (dt.date.today() - first_date).days or 1

  # 3. PnL Aggregation
  total_net_pnl = sum([float(c['total_pnl'] or 0) for c in cycles])

  # 4. ROI & CAGR
  roi_per_day = (total_net_pnl / days_active) / account_equity
  # Annualize using the standard compound formula
  projected_cagr = ((1 + roi_per_day)**252 - 1) * 100

  # 5. Spread Harvest Stats (Income Trades Only)
  all_income = list(app_tables.trades.search(
    role=config.ROLE_INCOME, status=config.STATUS_CLOSED,
    cycle=anvil.tables.query.any_of(*cycles)
  ))

  harvests = [t for t in all_income if (t['pnl'] or 0) > 0]
  harvest_rate = (len(harvests) / len(all_income)) * 100 if all_income else 0
  avg_win_pnl = (sum([t['pnl'] or 0 for t in harvests]) / len(harvests)) * 100 if harvests else 0

  # 6. Hedge Rent (The cost of the shield)
  all_hedges = list(app_tables.trades.search(
    role=config.ROLE_HEDGE, status=config.STATUS_CLOSED,
    cycle=anvil.tables.query.any_of(*cycles)
  ))
  net_hedge_impact = sum([(t['pnl'] or 0) * (t['quantity'] or 1) * 100 for t in all_hedges])

  return {
    'active': True,
    'total_pnl': round(total_net_pnl, 2),
    'days_active': days_active,
    'roi_day_pct': round(roi_per_day * 100, 3),
    'projected_cagr': round(projected_cagr, 1),
    'harvest_rate': round(harvest_rate, 1),
    'avg_win_dollars': round(avg_win_pnl, 2),
    'hedge_rent_total': round(net_hedge_impact, 2),
    'capital_utilized_avg': 10000 # Placeholder for now
  }

@anvil.server.callable
def get_strategic_efficiency() -> dict:
  """Calculates tactical KPIs and the EV Forecast model."""
  cycles = list(app_tables.cycles.search(status=config.STATUS_CLOSED, account=config.ACTIVE_ENV))
  if not cycles: return {}
  
  # 1. Gather all CLOSED Income Trades
  all_income = list(app_tables.trades.search(
    role=config.ROLE_INCOME, status=config.STATUS_CLOSED,
    cycle=anvil.tables.query.any_of(*cycles)
  ))
  
  if not all_income: 
    return {'active': False}
  
  # 2. Segment Wins vs. Losses (Stops/Roll Exits)
  # A Win is a trade where realized PnL > 0 (The Harvest)
  # A Loss is where PnL < 0 (The Stop/Roll Exit)
  wins = [t for t in all_income if (t['pnl'] or 0) > 0]
  losses = [t for t in all_income if (t['pnl'] or 0) < 0]
  
  # 3. Calculate Averages (Multiplied by 100 to get Dollars)
  avg_win_val = (sum([t['pnl'] for t in wins]) / len(wins)) * 100 if wins else 0
  avg_loss_val = (sum([t['pnl'] for t in losses]) / len(losses)) * 100 if losses else 0
  
  # Note: avg_loss_val will be a negative number (e.g., -250.00)
  harvest_rate = len(wins) / len(all_income)
  
  # 4. ACTUAL EV CALCULATION
  # Formula: (Win% * Avg_Win) + (Loss% * Avg_Loss)
  actual_ev = (harvest_rate * avg_win_val) + ((1 - harvest_rate) * avg_loss_val)
  
  # 5. THEORETICAL BASELINE (The "Standard_0DTE" math)
  # Assuming 0.15 Delta Target (85% Win Rate) and a 3x Credit Stop
  # Using $1.00 as a standard credit unit for comparison
  theory_win_rate = 0.85
  theory_win = 50.0   # 50% harvest on $1.00 credit
  theory_loss = -200.0 # Buying back at 3.00 (-2.00 loss)
  theoretical_ev = (theory_win_rate * theory_win) + ((1 - theory_win_rate) * theory_loss)
  
  return {
    'harvest_rate_pct': round(harvest_rate * 100, 1),
    'avg_win_dollars': round(avg_win_val, 2),
    'roll_stop_avg_dollars': round(abs(avg_loss_val), 2), # Show as positive 'Cost'
    'actual_ev': round(actual_ev, 2),
    'theoretical_ev': round(theoretical_ev, 2),
    'alpha': round(actual_ev - theoretical_ev, 2), # Performance vs. Math
    'trade_count': len(all_income)
  }

@anvil.server.callable
def get_equity_curve_data() -> dict:
  """Aggregates time-series data for the Cumulative PnL vs Capital Risked chart."""
  cycles = list(app_tables.cycles.search(
    status=config.STATUS_CLOSED, 
    account=config.ACTIVE_ENV
  ))

  if not cycles:
    return {'dates': [], 'cum_pnl': [], 'capital': []}

    # Sort chronologically
  cycles.sort(key=lambda x: x['end_date'] or dt.date.today())

  dates = []
  cum_pnl = []
  capital_risked = []

  running_pnl = 0.0

  for c in cycles:
    dates.append(c['end_date'])

    # Add to cumulative PnL
    running_pnl += float(c['total_pnl'] or 0)
    cum_pnl.append(round(running_pnl, 2))

    # Calculate Peak Capital Risked for this cycle
    trades = app_tables.trades.search(cycle=c, role=config.ROLE_INCOME)

    # Using the 'capital_required' DB column we established earlier
    cycle_cap = sum([float(t['capital_required'] or 0) for t in trades])
    capital_risked.append(cycle_cap)

  return {
    'dates': dates,
    'cum_pnl': cum_pnl,
    'capital': capital_risked
  }