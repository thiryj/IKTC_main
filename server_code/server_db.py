import anvil.server
from anvil.tables import app_tables
#import anvil.tables.query as q
#import anvil.secrets
#import anvil.tables as tables

from shared.classes import Cycle, Trade, Leg, Transaction
from shared import config

# --- READS (fetching and hydrating)---
def get_active_cycle():
  """
  Fetches the single 'OPEN' cycle and fully hydrates its graph
  (Cycle -> Trades -> Legs).
  Returns None if no open cycle exists.
  """
  cycle_row = app_tables.cycles.get(Status=config.STATUS_OPEN) # 1. Fetch the Parent Row
  if not cycle_row:
    return None
  cycle = Cycle(cycle_row)   # 2. Convert to Object
  _hydrate_cycle_children(cycle, cycle_row) # 3. Deep Hydration (Populate the children)
  return cycle

def get_cycle_by_id(cycle_id):
  """Fetches a specific cycle by DB ID (useful for UI routing)"""
  cycle_row = app_tables.cycles.get_by_id(cycle_id)
  if not cycle_row:
    return None
  cycle = Cycle(cycle_row)
  _hydrate_cycle_children(cycle, cycle_row)
  return cycle

def create_new_cycle(account: str, underlying: str, rule_set_row):
  """Creates a fresh Cycle row and returns the object."""
  row = app_tables.cycles.add_row(
    Account=account,
    Underlying=underlying,
    Status=config.STATUS_OPEN,
    NetPL=0.0,
    DailyHedgeRef=0.0,
    RuleSet=rule_set_row
  )
  return Cycle(row)

def save_cycle(cycle_obj):
  """Updates the Cycle row with values from the object."""
  if not cycle_obj.id:
    raise ValueError("Cannot save a Cycle that has no ID")

  row = app_tables.cycles.get_by_id(cycle_obj.id)
  row.update(
    Status=cycle_obj.status,
    NetPL=cycle_obj.net_pl,
    DailyHedgeRef=cycle_obj.daily_hedge_ref
    # Note: We rarely update 'Account' or 'Underlying' after creation
  )

def save_trade(trade_obj):
  """Updates the Trade row with values from the object."""
  if not trade_obj.id:
    # Create new if needed, or raise error. 
    # For now, let's assume we update existing trades.
    raise ValueError("Cannot save a Trade that has no ID")

  row = app_tables.trades.get_by_id(trade_obj.id)
  row.update(
    Status=trade_obj.status,
    EntryCredit=trade_obj.entry_credit,
    TotalPL=trade_obj.total_pl,
    RollTriggerPrice=trade_obj.roll_trigger,
    CapitalRequired=trade_obj.capital_req
  )

# --- INTERNAL HYDRATION HELPERS ---

def _hydrate_cycle_children(cycle, cycle_row):
  """return the trades and legs of this cycle in the cycle object"""
  trade_rows = app_tables.trades.search(Cycle=cycle_row)
  cycle.trades = []
  
  # Map and hydrate
  for t_row in trade_rows:
    trade_obj = Trade(t_row)
    
    # Hydrate legs
    leg_rows = app_tables.legs.search(Trade=t_row)
    trade_obj.legs = [Leg(l_row) for l_row in leg_rows]
    cycle.trades.append(trade_obj)
    
    # Link hedge
    if cycle_row['HedgeTrade'] and t_row.get_id() == cycle_row['HedgeTrade'].get_id():
      cycle.hedge_trade_link = trade_obj
