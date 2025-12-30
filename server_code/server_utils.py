import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

import datetime as dt

@anvil.server.callable
def print_entire_db_schema():
  print({k: [c['name'] for c in v.list_columns()] for k, v in {
    'Cycles': app_tables.cycles, 
    'Legs': app_tables.legs, 
    'Trades': app_tables.trades, 
    'Transactions': app_tables.transactions,
    'Settings': app_tables.settings,
    'RuleSets': app_tables.rulesets,
    'AutomationLogs': app_tables.automationlogs
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
def seed_db_for_test():
  # 1. Clear tables
  for t in [app_tables.legs, app_tables.transactions, app_tables.trades, app_tables.cycles]:
    t.delete_all_rows()
  # 2. Get/Create linked RuleSet row (assuming 'Name' column exists in RuleSets)
  rs_row = app_tables.rulesets.get(Name='2026-1') or app_tables.rulesets.add_row(Name='2026-1')

  # 3. Seed Cycle with linked row
  cycle = app_tables.cycles.add_row(
    Status='OPEN', Name='Test_Cycle_1', StartDate=dt.date.today(), RuleSet=rs_row
  )

  # 4. Seed Trade
  trade = app_tables.trades.add_row(
    Cycle=cycle, Underlying='SPX', Status='OPEN', OpenDate=dt.date.today(), Quantity=1
  )

  # 5. Seed Transaction
  txn = app_tables.transactions.add_row(
    Trade=trade, TradierOrderID='999999', TransactionDate=dt.date.today(), CreditDebit=500.0
  )

  # Helper to generate OCC Symbol: Root(6) + YYMMDD + Type(1) + Strike(8)
  def make_occ(root, date, type_char, strike):
    date_str = date.strftime('%y%m%d')
    strike_str = f"{int(strike*1000):08d}"
    return f"{root}{date_str}{type_char}{strike_str}"

  exp_date = dt.date(2026, 1, 16)

  # 6. Seed Legs with OCCSymbol
  # Short Leg
  app_tables.legs.add_row(
    Transaction=txn, Action='Sell', Strike=4000, OptionType='Put', 
    Expiration=exp_date, active=True, Underlying='SPX',
    OCCSymbol=make_occ('SPXW', exp_date, 'P', 4000)
  )
  # Long Leg
  app_tables.legs.add_row(
    Transaction=txn, Action='Buy', Strike=3950, OptionType='Put', 
    Expiration=exp_date, active=True, Underlying='SPX',
    OCCSymbol=make_occ('SPXW', exp_date, 'P', 3950)
  )

  return "Reset Complete: Linked RuleSet and seeded data."

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