import anvil.server
from anvil.tables import app_tables
import datetime as dt

from shared.classes import Cycle, Trade, Leg, Transaction
from shared import config

# --- READS (fetching and hydrating) ---

def get_active_cycle():
  """
  Fetches the single 'OPEN' cycle and fully hydrates its graph.
  Returns None if no open cycle exists.
  """
  # Schema: 'status' (snake_case)
  cycle_row = app_tables.cycles.get(status=config.STATUS_OPEN)
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
  
  # 1. Create the Trade Row
  trade_row = app_tables.trades.add_row(
    cycle=cycle_row,
    role=role,
    status=config.STATUS_OPEN,
    quantity=trade_dict['quantity'],
    entry_price=fill_price,
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
    price=fill_price,
    quantity=trade_dict['quantity'],
    fees=fees,
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
      expiry=short_data.get('expiration_date') or short_data.get('expiry'), 
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
    expiry=long_data.get('expiration_date') or long_data.get('expiry'),
    occ_symbol=long_data.get('symbol') or long_data.get('occ_symbol'),
    active=True
  )

  return Trade(trade_row)

# --- INTERNAL HYDRATION HELPERS ---

def _hydrate_cycle_children(cycle, cycle_row):
  """Populate trades and legs into the cycle object."""
  # Schema: 'cycle' (column in trades table)
  trade_rows = app_tables.trades.search(cycle=cycle_row)
  cycle.trades = []
  cycle.hedge_trade_link = None 
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