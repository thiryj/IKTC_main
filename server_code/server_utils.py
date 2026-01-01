import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

import datetime as dt

from shared import config

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

@anvil.server.callable
def setup_test_scenario():
  """
    Creates:
    1. A RuleSet (if not exists)
    2. An OPEN Cycle
    3. An ACTIVE Hedge Trade (Linked to Cycle)
    4. The Transactions/Legs for the Hedge
    """
  print("--- SEEDING DATABASE ---")

  # 1. Get or Create RuleSet
  rules = app_tables.rule_sets.get(name="Standard_0DTE")
  if not rules:
    print("Creating RuleSet...")
    rules = app_tables.rule_sets.add_row(
      name="Standard_0DTE",
      description="20 Delta / 25 Wide",
      spread_target_delta=0.20,
      spread_width=25,
      spread_min_premium=1.00,
      spread_max_premium=1.20,
      spread_size_factor=5.0, # 5 spreads per 1 hedge
      trade_start_delay=15,
      gap_down_thresh=0.02
      # Add other columns as needed by your schema
    )

    # 2. Create Cycle
  print("Creating Cycle...")
  cycle_row = app_tables.cycles.add_row(
    account="TEST_ACC",
    underlying="SPX",
    status=config.STATUS_OPEN,
    start_date=dt.date.today(),
    total_pnl=0.0,
    daily_hedge_ref=100.0, # Mock price
    rule_set=rules,
    notes="Automated Test Scenario"
  )

  # 3. Create Hedge Trade
  print("Creating Hedge Trade...")
  hedge_trade = app_tables.trades.add_row(
    cycle=cycle_row,
    role=config.ROLE_HEDGE,
    status=config.STATUS_OPEN,
    quantity=1, # 1 Hedge Contract
    entry_price=50.0,
    entry_time=dt.datetime.now(),
    pnl=0.0,
    order_id_external="SEED_HEDGE_1"
  )

  # 4. Link Hedge back to Cycle (CRITICAL STEP)
  cycle_row['hedge_trade'] = hedge_trade

  # 5. Create Hedge Legs (Optional for entry logic, but good for completeness)
  app_tables.legs.add_row(
    trade=hedge_trade,
    side=config.LEG_SIDE_LONG,
    quantity=1,
    strike=4000,
    option_type='put',
    expiry=dt.date.today() + dt.timedelta(days=90),
    active=True,
    occ_symbol="SPX_HEDGE_SEED"
  )

  print(f"--- SEED COMPLETE ---")
  print(f"Cycle ID: {cycle_row.get_id()}")
  print("You can now run 'run_automation_routine()'")

@anvil.server.callable
def make_position_dangerous(short_strike, long_strike):
  txn = app_tables.transactions.get(TradierOrderID='999999')
  # Get expiration from the first leg found
  existing_legs = app_tables.legs.search(Transaction=txn)
  if not len(existing_legs): return "Error: No legs found for this transaction."
  exp_date = existing_legs[0]['Expiration']

  def make_occ(strike):
    return f"SPXW{exp_date.strftime('%y%m%d')}P{int(strike*1000):08d}"

    # Handle 'Short' OR 'Sell'
  short_leg = app_tables.legs.get(Transaction=txn, Action='Short') or app_tables.legs.get(Transaction=txn, Action='Sell')
  if short_leg: short_leg.update(Strike=short_strike, OCCSymbol=make_occ(short_strike))

    # Handle 'Long' OR 'Buy'
  long_leg = app_tables.legs.get(Transaction=txn, Action='Long') or app_tables.legs.get(Transaction=txn, Action='Buy')
  if long_leg: long_leg.update(Strike=long_strike, OCCSymbol=make_occ(long_strike))

  return f"Updated: Short {short_strike} / Long {long_strike}"

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