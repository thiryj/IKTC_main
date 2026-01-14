import anvil.email
import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

import datetime as dt

from shared import config
from . import server_api, server_db

@anvil.server.callable
def print_entire_db_schema():
  print({k: [c['name'] for c in v.list_columns()] for k, v in {
    'cycles': app_tables.cycles, 
    'legs': app_tables.legs, 
    'trades': app_tables.trades, 
    'transactions': app_tables.transactions,
    'settings': app_tables.settings,
    'rule_sets': app_tables.rule_sets,
    'logs': app_tables.logs
  }.items()})

@anvil.server.callable
def print_selected_table_schemas(*table_names)->str:
  #print({k: [c['name'] for c in v.list_columns()] for k, v in {table_name: eval(f"app_tables.{table_name}")}.items()})
  print({tn: [c['name'] for c in getattr(app_tables, tn).list_columns()] for tn in table_names})

@anvil.server.callable
def factory_reset():
  # 1. Clear tables
  for t in [app_tables.legs, app_tables.transactions, app_tables.trades, app_tables.cycles]:
    t.delete_all_rows()


# populate rule_sets row
@anvil.server.callable
def populate_default_rules():
  app_tables.rule_sets.add_row(
    name="Standard_0DTE",

    # Hedge Rules (Longer dated puts)
    hedge_target_delta=0.25,
    hedge_target_dte=90,
    hedge_alloc_pct=0.05,
    hedge_min_dte=60,
    hedge_min_delta=0.15,
    hedge_max_delta=0.4,

    # Spread Entry (The 0DTE Engine)
    spread_target_delta=0.20,   # ~10 Delta short strikes
    spread_width=25,            # Standard $25 wide wings for SPX
    spread_target_dte=0,        # 0DTE
    spread_min_premium=0.80,    # Minimum credit to enter
    spread_max_premium=2.0,    # Avoid super volatile entries
    spread_size_factor=5,     # Multiplier for sizing
    trade_start_delay=15,        # Minutes after open to wait
    gap_down_thresh=.15,        # % gap down to pause trading

    # Spread Management
    roll_trigger_mult=3.0,      # Roll if price hits 2x credit? (Or strike touch)
    roll_max_debit=.1,         # Max debit pay to fix a trade
    profit_target_pct=0.50,     # Take profit at 50%

    # Safety
    panic_threshold_dpu=350.0    # dollars per unit to trigger a cycle liquidation event
  )
  print("Default rules populated.")

# delete specific rows from table

@anvil.server.background_task
def delete_logs_task(text:str):
  for row in app_tables.logs.search(message=q.ilike(f"{text}%")):
    row.delete()

@anvil.server.callable
def delete_logs_by_message(text):
  """Launch the background task and return immediately."""
  anvil.server.launch_background_task('delete_logs_task', text)

@anvil.server.callable
def list_open_trades():
  print("--- OPEN TRADES IN DB ---")
  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
  if not cycle:
    print("No active cycle.")
    return

  for t in app_tables.trades.search(cycle=cycle._row, status=config.STATUS_OPEN):
    legs = app_tables.legs.search(trade=t)
    leg_str = ", ".join([l['occ_symbol'] for l in legs])
    print(f"ID: {t.get_id()} | Role: {t['role']} | Entry: {t['entry_price']} | Legs: {leg_str}")

@anvil.server.callable
def manual_db_close(trade_row_id_string, exit_price):
  """
    Manually closes a trade in the DB (Surgery).
    Does NOT call the API.
    Run manual_db_close("[1234,5678]", 0.85) (or whatever your fill price was).
    """
  print(f"Executing Manual DB Close for {trade_row_id_string} at ${exit_price}...")

  # Fetch row by ID
  trade_row = app_tables.trades.get_by_id(trade_row_id_string)

  if not trade_row:
    print("Error: Trade row not found.")
    return

  if trade_row['status'] != config.STATUS_OPEN:
    print(f"Warning: Trade is already {trade_row['status']}.")

    # Use the existing close logic in server_db to handle legs/transactions/pnl
  server_db.close_trade(
    trade_row=trade_row,
    fill_price=float(exit_price),
    fill_time=dt.datetime.now(),
    order_id="MANUAL_ADMIN_CLOSE",
    fees=0.0
  )
  print("Success. Trade closed.")

# 1. Get raw positions
positions = server_api.get_current_positions()
print(f"Total Positions reported: {len(positions)}")

for p in positions:
  print(f"Symbol: {p.get('symbol')} | Qty: {p.get('quantity')} | ID: {p.get('id')}")