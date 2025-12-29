import anvil.server
from anvil.tables import app_tables
#import anvil.tables.query as q
#import anvil.secrets
#import anvil.tables as tables

from shared.classes import Cycle, Trade, Leg, Transaction
from shared import config

def get_active_cycle():
  """
    Fetches the single 'OPEN' cycle and fully hydrates its graph
    (Cycle -> Trades -> Legs).
    Returns None if no open cycle exists.
    """
  # 1. Fetch the Parent Row
  cycle_row = app_tables.cycles.get(Status=config.STATUS_OPEN)

  if not cycle_row:
    return None

    # 2. Convert to Object
  cycle = Cycle(cycle_row)

  # 3. Deep Hydration (Populate the children)
  _hydrate_cycle_children(cycle, cycle_row)

  return cycle

def get_cycle_by_id(cycle_id):
  """
    Fetches a specific cycle by DB ID (useful for UI routing).
    """
  cycle_row = app_tables.cycles.get_by_id(cycle_id)
  if not cycle_row:
    return None

  cycle = Cycle(cycle_row)
  _hydrate_cycle_children(cycle, cycle_row)
  return cycle

def create_new_cycle(account_name, underlying_symbol, rule_set_row):
  """
    Creates a fresh Cycle in the database with status 'NEW'.
    """
  # 1. Insert Row
  row = app_tables.cycles.add_row(
    Account=account_name,
    Underlying=underlying_symbol,
    Status=config.STATUS_NEW,
    NetPL=0.0,
    DailyHedgeRef=0.0,
    RuleSet=rule_set_row
  )

  # 2. Return Hydrated Object
  return Cycle(row)

# --- INTERNAL HYDRATION HELPERS ---

def _hydrate_cycle_children(cycle, cycle_row):
  """
    Wiring the Object Graph:
    1. Fetch all Trades for this Cycle.
    2. Fetch all Legs for those Trades.
    3. Assemble them into the cycle object.
    """
  # --- A. FETCH TRADES ---
  # Query: Get all trades pointing to this cycle
  trade_rows = app_tables.trades.search(Cycle=cycle_row)

  # Convert Rows -> Objects
  trades_map = {} # Map ID -> TradeObject for linking later
  cycle.trades = []

  for t_row in trade_rows:
    trade_obj = Trade(t_row)
    trade_obj.legs = [] # Prepare container for grandkids

    cycle.trades.append(trade_obj)
    trades_map[t_row.get_id()] = trade_obj

    # --- B. HANDLE SPECIAL LINKS ---
    # The Cycle table has a specific link 'HedgeTrade' pointing to one of these trades.
    # We need to replace the raw Row with the actual Object we just created.
  if cycle_row['HedgeTrade']:
    hedge_row_id = cycle_row['HedgeTrade'].get_id()
    if hedge_row_id in trades_map:
      cycle.hedge_trade_link = trades_map[hedge_row_id]
    else:
      # Edge case: Hedge trade might be archived or missing
      cycle.hedge_trade_link = None

    # --- C. FETCH LEGS (Grandchildren) ---
    # Optimization: Fetch ALL legs for these trades in one query if possible,
    # or iterate. Since Anvil search is lazy, iterating is okay for <100 items.

    # Gather all Trade rows to search against
    # (Anvil 'q.any_of' is useful here if you have many trades, 
    # but a simple loop is often readable enough for small datasets)

  for trade_obj in cycle.trades:
    # We use the row stored inside the object to find its legs
    # Note: We must access the internal row. 
    # Since 'Trade' class stores the row? 
    # Wait, our shared class didn't explicitly store 'self.row'. 
    # We should fix that or pass the row in the map.

    # FIX: We can fetch using the Link in the Legs table
    # We need the ROW for the trade, not the object.
    # But our Trade object (from shared.classes) currently discards the row.
    pass 

    # --- REVISION ON HYDRATION ---
    # To fetch legs efficiently, we need the Trade Rows.
    # Let's do it inside the loop above while we still had the 't_row'.

    # Let's re-write the loop for clarity:
  cycle.trades = []

  for t_row in trade_rows:
    trade_obj = Trade(t_row)

    # Fetch Legs for this specific trade
    leg_rows = app_tables.legs.search(Trade=t_row)
    trade_obj.legs = [Leg(l_row) for l_row in leg_rows]

    cycle.trades.append(trade_obj)

    # Check if this is the hedge
    if cycle_row['HedgeTrade'] and t_row.get_id() == cycle_row['HedgeTrade'].get_id():
      cycle.hedge_trade_link = trade_obj
