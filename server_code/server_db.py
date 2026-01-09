import anvil.server
import anvil.tables
from anvil.tables import app_tables
import datetime as dt

from shared.classes import Cycle, Trade, Leg, Transaction
from shared import config
from . import server_logging as logger

def _fmt(val):
  """Rounds price to 2 decimal places for clean DB storage."""
  if val is None: return None
  return round(float(val), 2)

# --- READS (fetching and hydrating) ---

def get_active_cycle(env_account: str)-> Cycle | None:
  """
  Fetches the single 'OPEN' cycle for the active ENV and fully hydrates its graph.
  Returns None if no open cycle exists.
  """
  
  cycle_row = app_tables.cycles.get(
    status=config.STATUS_OPEN,
    account=env_account
  )
  if not cycle_row:
    return None

  cycle = Cycle(cycle_row)
  _hydrate_cycle_children(cycle, cycle_row)
  return cycle

def get_cycle_by_id(cycle_id):
  """Fetches a specific cycle by DB ID."""
  cycle_row = app_tables.cycles.get_by_id(cycle_id)
  if not cycle_row:
    return None

  cycle = Cycle(cycle_row)
  _hydrate_cycle_children(cycle, cycle_row)
  return cycle

def get_scaled_rules(rule_set_name, symbol):
  # 1. Fetch SPX-standard rules
  rules_row = app_tables.rule_sets.get(name=rule_set_name)
  if not rules_row:
    return None
  rules = dict(rules_row)

  # 2. Apply "Sandbox Patch" if trading SPY (Scale Price/Width by 1/10th)
  if symbol == 'SPY':
    keys_to_scale = [
      'spread_width', 
      'spread_min_premium', 
      'spread_max_premium', 
      'roll_max_debit', 
      'panic_threshold_dpu'
    ]

    for k in keys_to_scale:
      if rules.get(k) is not None:
        rules[k] = rules[k] / 10.0

  return rules

# --- DB WRITES (Creates, Saves, Updates) ---

def create_new_cycle(account: str, underlying: str, rule_set_row):
  """Creates a fresh Cycle row and returns the object."""
  # Schema: snake_case columns
  row = app_tables.cycles.add_row(
    account=account,
    underlying=underlying,
    status=config.STATUS_OPEN,
    total_pnl=0.0,          # Schema: total_pnl (was NetPL)
    daily_hedge_ref=0.0,    # Schema: daily_hedge_ref (was DailyHedgeRef)
    rule_set=rule_set_row,  # Schema: rule_set
    start_date=dt.date.today()
  )
  return Cycle(row)

def save_cycle(cycle_obj):
  """Updates the Cycle row with values from the object."""
  if not cycle_obj.id:
    raise ValueError("Cannot save a Cycle that has no ID")

  row = app_tables.cycles.get_by_id(cycle_obj.id)

  # Schema matching
  row.update(
    status=cycle_obj.status,
    total_pnl=cycle_obj.total_pnl,
    daily_hedge_ref=cycle_obj.daily_hedge_ref,
    notes=cycle_obj.notes
  )

def save_trade(trade_obj):
  """Updates the Trade row with values from the object."""
  if not trade_obj.id:
    raise ValueError("Cannot save a Trade that has no ID")

  row = app_tables.trades.get_by_id(trade_obj.id)

  # Schema matching
  row.update(
    status=trade_obj.status,
    entry_price=trade_obj.entry_price,       # Schema: entry_price
    pnl=trade_obj.pnl,                       # Schema: pnl
    roll_trigger_price=trade_obj.roll_trigger_price, # Schema: roll_trigger_price
    capital_required=trade_obj.capital_required,     # Schema: capital_required
    exit_price=trade_obj.exit_price,
    exit_time=trade_obj.exit_time
  )

@anvil.tables.in_transaction
def record_new_trade(
  cycle_row,
  role: str,
  trade_dict: dict,
  order_id: str,
  fill_price: float,
  fill_time: dt.datetime,
  fees: float = 0.0
) -> Trade:
  """
  Persists a fully executed trade to the database.
  Creates: Trade -> Transaction -> Legs (1 or 2 depending on role).
  """
  rules = cycle_row['rule_set']  #needed for exit strat calcs
  
  def _parse_date(val):
    if not val: return None
    if isinstance(val, dt.date): return val
    if isinstance(val, dt.datetime): return val.date()
    if isinstance(val, str):
      try:
        return dt.datetime.strptime(val, "%Y-%m-%d").date()
      except ValueError:
        return None
    return None
  
  # 1. Create the Trade Row
  trade_row = app_tables.trades.add_row(
    cycle=cycle_row,
    role=role,
    status=config.STATUS_OPEN,
    quantity=trade_dict['quantity'],
    entry_price=_fmt(fill_price),
    entry_time=fill_time,
    order_id_external=order_id,
    pnl=0.0,

    # Strategy Logic: Set targets based on role
    target_harvest_price=fill_price * rules['profit_target_pct'] if role == config.ROLE_INCOME else None,
    roll_trigger_price=fill_price * rules['roll_trigger_mult'] if role == config.ROLE_INCOME else None,

    # Calculate capital required (Width * 100 * Qty)
    capital_required=(
      trade_dict['quantity'] * config.DEFAULT_MULTIPLIER * abs(trade_dict['short_strike'] - trade_dict['long_strike']) 
      if role == config.ROLE_INCOME else 0.0
    )
  )

  # 2. Record the Opening Transaction
  open_txn = app_tables.transactions.add_row(
    trade=trade_row,
    action="OPEN_SPREAD" if role == config.ROLE_INCOME else "OPEN_HEDGE",
    price=_fmt(fill_price),
    quantity=trade_dict['quantity'],
    fees=_fmt(fees),
    timestamp=fill_time,
    order_id_external=order_id
  )

  # 3. Create Leg Rows
  short_data = trade_dict['short_leg_data']
  long_data = trade_dict['long_leg_data']

  # Short Leg
  
  if role == config.ROLE_INCOME and short_data:
    app_tables.legs.add_row(
      trade=trade_row,
      opening_transaction=open_txn,
      closing_transaction=None,
      side=config.LEG_SIDE_SHORT,
      quantity=trade_dict['quantity'],
      strike=trade_dict['short_strike'],
      option_type=config.TRADIER_OPTION_TYPE_PUT,
      expiry=_parse_date(short_data.get('expiration_date') or short_data.get('expiry')), 
      occ_symbol=short_data.get('symbol') or short_data.get('occ_symbol'),
      active=True
    )

  # Long Leg
  app_tables.legs.add_row(
    trade=trade_row,
    opening_transaction=open_txn,
    closing_transaction=None,
    side=config.LEG_SIDE_LONG,
    quantity=trade_dict['quantity'],
    strike=trade_dict['long_strike'],
    option_type=config.TRADIER_OPTION_TYPE_PUT,
    expiry=_parse_date(long_data.get('expiration_date') or long_data.get('expiry')),
    occ_symbol=long_data.get('symbol') or long_data.get('occ_symbol'),
    active=True
  )
  return Trade(trade_row)

@anvil.tables.in_transaction
def close_trade(trade_row, fill_price: float, fill_time: dt.datetime, order_id: str, fees: float = 0.0):
  """Finalizes a trade: Records closing transaction, deactivates legs, updates trade status/PnL"""
  # 1. Determine Action String
  action_type = "CLOSE_SPREAD" if trade_row['role'] == config.ROLE_INCOME else "CLOSE_HEDGE"

  # 2. Create Closing Transaction
  close_txn = app_tables.transactions.add_row(
    trade=trade_row,
    action=action_type,
    price=_fmt(fill_price),
    quantity=trade_row['quantity'],
    fees=_fmt(fees),
    timestamp=fill_time,
    order_id_external=order_id
  )

  # 3. Update Legs (Deactivate & Link)
  # Fetch all active legs linked to this trade
  legs = app_tables.legs.search(trade=trade_row, active=True)
  for leg in legs:
    leg['active'] = False
    leg['closing_transaction'] = close_txn

    # 4. Update Trade Row
  entry_price = trade_row['entry_price'] or 0.0

  # Calculate PnL based on Role
  if trade_row['role'] == config.ROLE_INCOME:
    # Credit Spread: Profit = Entry Credit - Exit Debit
    pnl = entry_price - fill_price
  else:
    # Long Hedge: Profit = Exit Credit - Entry Debit
    pnl = fill_price - entry_price

  trade_row.update(
    status=config.STATUS_CLOSED,
    exit_price=_fmt(fill_price),
    exit_time=fill_time,
    pnl=_fmt(pnl)
  )
  
@anvil.tables.in_transaction
def settle_zombie_trade(trade_row):
  """
    Settles a missing trade using WORST CASE assumption.
    - Income Spread: Assumes Max Loss (Exit Price = Strike Width).
    - Hedge: Assumes Expired Worthless (Exit Price = 0).
    User must manually reconcile with the actual overnight outcome.
    """
  
  logger.log(f"DB: Settling Zombie Trade {trade_row.get_id()} as MAX LOSS.", 
             level=config.LOG_WARNING, 
             source=config.LOG_SOURCE_DB, 
             context={trade_row.get_id()}
            )

  # 1. Determine Worst Case Exit Price
  exit_price = 0.0

  if trade_row['role'] == config.ROLE_INCOME:
    # For a credit spread, Max Loss happens if we buy it back at full width
    # Fetch legs to calculate width
    legs = app_tables.legs.search(trade=trade_row)
    short_leg = next((l for l in legs if l['side'] == config.LEG_SIDE_SHORT), None)
    long_leg = next((l for l in legs if l['side'] == config.LEG_SIDE_LONG), None)

    if short_leg and long_leg:
      width = abs(short_leg['strike'] - long_leg['strike'])
      exit_price = width
    else:
      # Data corruption fallback: Assume a painful default (e.g. $5.00) or 0
      exit_price = 0.0 
  else:
    # For a long Hedge, worst case is expiring worthless ($0.00)
    exit_price = 0.0

    # 2. Record "Administrative" Transaction
  app_tables.transactions.add_row(
    trade=trade_row,
    action="ZOMBIE_SETTLE",
    price=_fmt(exit_price),
    quantity=trade_row['quantity'],
    timestamp=dt.datetime.now(),
    order_id_external="MANUAL_AUDIT_REQ"
  )

  # 3. Deactivate Legs
  for leg in app_tables.legs.search(trade=trade_row, active=True):
    leg['active'] = False

    # 4. Update Trade Row
  entry_price = trade_row['entry_price'] or 0.0

  if trade_row['role'] == config.ROLE_INCOME:
    pnl = entry_price - exit_price # e.g., 0.50 - 2.50 = -2.00
  else:
    pnl = exit_price - entry_price # e.g., 0.00 - 5.00 = -5.00

  trade_row.update(
    status=config.STATUS_CLOSED,
    exit_price=_fmt(exit_price),
    exit_time=dt.datetime.now(),
    pnl=_fmt(pnl),
    notes=f"{trade_row['notes'] or ''} [ZOMBIE: MAX LOSS APPLIED]"
  )

# --- INTERNAL HYDRATION HELPERS ---

def _hydrate_cycle_children(cycle, cycle_row):
  """Populate trades and legs into the cycle object."""
  # Schema: 'cycle' (column in trades table)
  trade_rows = app_tables.trades.search(cycle=cycle_row)
  cycle.hedge_trade_link = None 
  cycle.trades = []
  for t_row in trade_rows:
    trade_obj = Trade(t_row)

    # Schema: 'trade' (column in legs table)
    leg_rows = app_tables.legs.search(trade=t_row)
    trade_obj.legs = [Leg(l_row) for l_row in leg_rows]
    cycle.trades.append(trade_obj)

    # Link hedge
    # Schema: 'hedge_trade' (column in cycles table)
    if cycle_row['hedge_trade'] and t_row.get_id() == cycle_row['hedge_trade'].get_id():
      cycle.hedge_trade_link = trade_obj