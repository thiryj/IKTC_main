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
  '''
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
  '''
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

@anvil.server.callable
def seed_panic_scenario():
  print("--- SEEDING PANIC SCENARIO ---")

  # 1. Fetch Valid Symbols (for API compatibility)
  target_date = dt.date.today() + dt.timedelta(days=10)
  chain = server_api.get_option_chain(date=target_date)
  if not chain:
    print("Error: No chain found to seed symbols.")
    return

  puts = [o for o in chain if o.get('option_type') == 'put']
  if len(puts) < 3: return
  puts.sort(key=lambda x: x['strike'], reverse=True)

  # Pick Hedge (Way OTM) and Spread (ATM)
  hedge_leg = puts[-1] # Lowest strike
  short_leg = puts[0]
  long_leg = puts[1]

  # 2. Create Cycle with "Magic" Reference Price
  print("Creating Cycle with manipulated Hedge Reference...")
  rules = app_tables.rule_sets.get(name="Standard_0DTE")

  cycle_row = app_tables.cycles.add_row(
    account="TEST_ACC",
    underlying="SPY",
    status=config.STATUS_OPEN,
    start_date=dt.date.today(),
    total_pnl=0.0,
    daily_hedge_ref=-50.0, # <--- THE TRICK: Forces a +$50 calculation
    rule_set=rules,
    notes="Panic Test"
  )

  # 3. Create Hedge Trade
  hedge_trade = app_tables.trades.add_row(
    cycle=cycle_row,
    role=config.ROLE_HEDGE,
    status=config.STATUS_OPEN,
    quantity=1,
    entry_price=1.0,
    entry_time=dt.datetime.now(),
    pnl=0.0,
    order_id_external="SEED_PANIC_HEDGE"
  )
  cycle_row['hedge_trade'] = hedge_trade

  # Hedge Leg
  app_tables.legs.add_row(
    trade=hedge_trade,
    side=config.LEG_SIDE_LONG,
    quantity=1,
    strike=hedge_leg['strike'],
    option_type='put',
    expiry=dt.datetime.strptime(hedge_leg['expiration_date'], "%Y-%m-%d").date(),
    occ_symbol=hedge_leg['symbol'],
    active=True
  )

  # 4. Create Spread Trade
  spread_trade = app_tables.trades.add_row(
    cycle=cycle_row,
    role=config.ROLE_INCOME,
    status=config.STATUS_OPEN,
    quantity=1,
    entry_price=0.50,
    entry_time=dt.datetime.now(),
    pnl=0.0,
    order_id_external="SEED_PANIC_SPREAD",
    target_harvest_price=0.25,
    roll_trigger_price=1.50
  )

  # Spread Legs
  txn = app_tables.transactions.add_row(trade=spread_trade, action="OPEN", price=0.50, quantity=1, timestamp=dt.datetime.now())

  app_tables.legs.add_row(trade=spread_trade, opening_transaction=txn, side=config.LEG_SIDE_SHORT, quantity=1, strike=short_leg['strike'], option_type='put', occ_symbol=short_leg['symbol'], expiry=dt.datetime.strptime(short_leg['expiration_date'], "%Y-%m-%d").date(), active=True)
  app_tables.legs.add_row(trade=spread_trade, opening_transaction=txn, side=config.LEG_SIDE_LONG, quantity=1, strike=long_leg['strike'], option_type='put', occ_symbol=long_leg['symbol'], expiry=dt.datetime.strptime(long_leg['expiration_date'], "%Y-%m-%d").date(), active=True)

  print("Seed Complete. Hedge Ref is -50.0. Panic should trigger.")

@anvil.server.callable
def seed_fresh_cycle():
  print("--- SEEDING FRESH CYCLE (SMART SCAN) ---")

  # 1. Clean up old mess (Optional)
  # app_tables.legs.delete_all_rows()
  # app_tables.transactions.delete_all_rows()
  # app_tables.trades.delete_all_rows()
  # app_tables.cycles.delete_all_rows()

  # 2. SCAN for a valid Chain (Sandbox friendly)
  # We look for ANY chain between 45 and 120 days out.
  # We step by 1 day because we don't know which specific Fridays exist in Sandbox.
  found_chain = []
  found_date = None

  print("Scanning for valid expiration (45-120 DTE)...")
  for d in range(45, 120):
    check_date = dt.date.today() + dt.timedelta(days=d)
    # Only check Fridays to save API calls (usually where the volume is)
    if check_date.weekday() != 4: 
      continue

    print(f"Checking {check_date}...", end="\r")
    chain = server_api.get_option_chain(date=check_date)
    if chain:
      found_chain = chain
      found_date = check_date
      print(f"\nFOUND valid chain at {check_date} (DTE: {d})")
      break

  if not found_chain:
    print("\nCRITICAL: No valid option chain found in scan range.")
    return

    # Filter Puts
  puts = [o for o in found_chain if o.get('option_type') == 'put']
  if not puts: 
    print("Chain found but no Puts inside.")
    return

    # Sort High Strike -> Low Strike
  puts.sort(key=lambda x: x['strike'], reverse=True)

  # Pick a "Hedge-like" leg (approx 25 delta or mid-pack)
  hedge_leg = puts[len(puts)//2]

  # --- TOGGLE FOR MAINTENANCE TEST ---
  # Uncomment next line to force the bot to think the hedge is expiring tomorrow
  # hedge_leg['expiration_date'] = (dt.date.today() + dt.timedelta(days=1)).strftime('%Y-%m-%d')
  # -----------------------------------

  print(f"Selected Hedge: {hedge_leg['symbol']}")

  # 3. Create Cycle
  rules = app_tables.rule_sets.get(name="Standard_0DTE")
  if not rules:
    print("Error: 'Standard_0DTE' rule set missing.")
    return

  current_hedge_price = float(hedge_leg.get('ask', 1.0) or 1.0)

  cycle_row = app_tables.cycles.add_row(
    account="TEST_ACC",
    underlying="SPY", 
    status=config.STATUS_OPEN,
    start_date=dt.date.today(),
    total_pnl=0.0,
    daily_hedge_ref=current_hedge_price, 
    rule_set=rules,
    notes="Fresh Start Seed"
  )

  # 4. Create Hedge Trade
  hedge_trade = app_tables.trades.add_row(
    cycle=cycle_row,
    role=config.ROLE_HEDGE,
    status=config.STATUS_OPEN,
    quantity=1,
    entry_price=current_hedge_price,
    entry_time=dt.datetime.now(),
    pnl=0.0,
    order_id_external="SEED_FRESH_HEDGE"
  )
  cycle_row['hedge_trade'] = hedge_trade

  # 5. Create Hedge Leg
  txn = app_tables.transactions.add_row(
    trade=hedge_trade, 
    action="OPEN_HEDGE", 
    price=current_hedge_price, 
    quantity=1, 
    timestamp=dt.datetime.now()
  )

  def parse_date(val):
    if isinstance(val, str): return dt.datetime.strptime(val, "%Y-%m-%d").date()
    return val

  app_tables.legs.add_row(
    trade=hedge_trade,
    opening_transaction=txn,
    side=config.LEG_SIDE_LONG,
    quantity=1,
    strike=hedge_leg['strike'],
    option_type='put',
    expiry=parse_date(hedge_leg['expiration_date']),
    occ_symbol=hedge_leg['symbol'],
    active=True
  )

  print("Seed Complete.")

@anvil.server.callable
def seed_active_spread():
  print("--- SEEDING ACTIVE SPREAD (SMART) ---")

  # 1. Get Context
  cycle = server_db.get_active_cycle()
  if not cycle:
    print("Error: No Active Cycle found. Run 'seed_fresh_cycle' first.")
    return

  if not cycle.hedge_trade_link:
    print("Error: Cycle exists but has no Hedge.")
    return

    # 2. Fetch Real Chain (Target ~7 days out for liquidity)
    # We want real symbols so the API accepts the Close order later
  target_date = dt.date.today() + dt.timedelta(days=7)
  chain = server_api.get_option_chain(date=target_date)

  # Fallback scan if specific date is empty in Sandbox
  if not chain:
    for d in range(3, 15):
      check_date = dt.date.today() + dt.timedelta(days=d)
      chain = server_api.get_option_chain(date=check_date)
      if chain: break

  if not chain or len(chain) < 2:
    print("CRITICAL: No valid option chain found to seed from.")
    return

    # 3. Pick Strikes (Puts)
  puts = [o for o in chain if o.get('option_type') == 'put']
  if len(puts) < 2: return

    # Sort High to Low
  puts.sort(key=lambda x: x['strike'], reverse=True)

  # Pick "Short" and "Long" (ATM or slightly OTM)
  # Just picking the first two available for the test
  short_leg = puts[0]
  long_leg = puts[1]

  print(f"Selected Legs: {short_leg['symbol']} / {long_leg['symbol']}")

  # 4. Create DB Records
  entry_credit = 0.50
  qty = 1

  trade_row = app_tables.trades.add_row(
    cycle=cycle._row,
    role=config.ROLE_INCOME,
    status=config.STATUS_OPEN,
    quantity=qty,
    entry_price=entry_credit,
    entry_time=dt.datetime.now(),
    pnl=0.0,
    order_id_external="SEED_SPREAD_TEST",

    # Targets
    target_harvest_price=entry_credit * 0.50, # 0.25
    roll_trigger_price=entry_credit * 3.0,    # 1.50

    capital_required=qty * 100 * abs(short_leg['strike'] - long_leg['strike'])
  )

  txn = app_tables.transactions.add_row(
    trade=trade_row,
    action="OPEN_SPREAD",
    price=entry_credit,
    quantity=qty,
    timestamp=dt.datetime.now(),
    order_id_external="SEED_SPREAD_TEST"
  )

  # Helper date parser
  def parse_d(val):
    if isinstance(val, str): return dt.datetime.strptime(val, "%Y-%m-%d").date()
    return val

    # Short Leg
  app_tables.legs.add_row(
    trade=trade_row,
    opening_transaction=txn,
    side=config.LEG_SIDE_SHORT,
    quantity=qty,
    strike=short_leg['strike'],
    option_type='put',
    expiry=parse_d(short_leg.get('expiration_date')),
    occ_symbol=short_leg['symbol'],
    active=True
  )

  # Long Leg
  app_tables.legs.add_row(
    trade=trade_row,
    opening_transaction=txn,
    side=config.LEG_SIDE_LONG,
    quantity=qty,
    strike=long_leg['strike'],
    option_type='put',
    expiry=parse_d(long_leg.get('expiration_date')),
    occ_symbol=long_leg['symbol'],
    active=True
  )

  print("Seed Complete. Active Spread created.")
  print(f"Roll Trigger: {trade_row['roll_trigger_price']}")
  print(f"Harvest Target: {trade_row['target_harvest_price']}")