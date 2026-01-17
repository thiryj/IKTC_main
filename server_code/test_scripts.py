import anvil.email
import anvil.server
import unittest
from unittest.mock import MagicMock, patch
import datetime as dt
from io import StringIO

from shared import config
from . import server_libs
from . import server_api
from . import server_main
from . import server_db

#from . server_client import start_new_cycle, get_campaign_dashboard, run_auto, close_campaign_manual
from anvil.tables import app_tables

@anvil.server.callable
def diagnostic_preflight() -> str:
  print("--- DIAGNOSTIC: PRE-FLIGHT CHECK ---")

  # 1. Test Heartbeat Update
  s = app_tables.settings.get()
  s['last_bot_heartbeat'] = dt.datetime.now()
  print(f"Heartbeat updated to: {s['last_bot_heartbeat']}")

  # 2. Test Scaling Logic
  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
  if cycle:
    print(f"Active Env: {config.ACTIVE_ENV} | Underlying: {cycle.underlying}")

    # Access the rules property (which we optimized to a cached dict)
    rules = cycle.rules 
    width = rules.get('spread_width')

    db_rules = app_tables.rule_sets.get(name=config.ACTIVE_RULESET)
    print(f"Logic Check: DB Width={db_rules['spread_width']} -> Effective Width={width}")

    if cycle.underlying == 'SPY' and width != round(db_rules['spread_width'] / 10):
      print("FAILURE: Scaling not applied correctly to SPY.")
    else:
      print("SUCCESS: Scaling logic verified.")
  else:
    print("NOTICE: No active cycle found to test scaling.")

  return "Pre-flight complete. Check Server Console."

@anvil.server.callable
def diagnostic_batch_api() -> dict:
  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
  if not cycle: 
    return {"error": "No active cycle"}

  start_time = dt.datetime.now()
  snapshot = server_api.get_market_data_snapshot(cycle)
  end_time = dt.datetime.now()

  duration = (end_time - start_time).total_seconds()

  print(f"--- BATCH API TEST ---")
  print(f"Duration: {duration:.2f} seconds")
  print(f"Underlying: {snapshot.get('price')}")
  print(f"Hedge Last: {snapshot.get('hedge_last')}")
  print(f"Spread Marks: {snapshot.get('spread_marks')}")

  return {
    "duration": duration,
    "underlying": snapshot.get('price'),
    "marks_count": len(snapshot.get('spread_marks', {}))
  }
'''
class TestScanner(unittest.TestCase):

  def setUp(self):
    # This runs automatically BEFORE every single test.
    # It creates a fresh "Fake Tradier" so one test doesn't mess up another.
    self.mock_t = MagicMock()
    self.account_id = "TEST_ACCT_123"

    # Create a date 60 days in the future for testing valid DTE
    self.future_date = dt.date.today() + dt.timedelta(days=60)
    self.future_date_str = self.future_date.strftime("%Y-%m-%d")

  @patch('server_helpers.app_tables')
  def test_existing_cycle_skips_scan(self, mock_db):
    """
        Scenario: DB says a cycle is already OPEN.
        Expected: Function returns that row immediately and DOES NOT call Tradier.
        """
    # --- FIX START ---
    # 1. Create a "Smart" Mock instead of a plain dict
    fake_cycle = MagicMock()

    # 2. Teach it to respond to .get_id()
    fake_cycle.get_id.return_value = 'existing_cycle_row'

    # 3. Teach it to act like a dictionary (optional, but good practice for rows)
    # This lets you do fake_cycle['Status'] if needed later
    fake_data = {'Status': 'OPEN'}
    fake_cycle.__getitem__.side_effect = fake_data.__getitem__

    mock_db.cycles.get.return_value = fake_cycle
    # --- FIX END ---

    # Run Function
    result = scan_and_initialize_cycle(self.mock_t, self.account_id)

    # Verify
    self.assertEqual(result, fake_cycle)
    mock_db.cycles.get.assert_called_with(Status='OPEN')

    # CRITICAL CHECK
    self.mock_t.account.get_positions.assert_not_called()

  @patch('server_helpers.app_tables')
  def test_no_valid_hedge_found(self, mock_db):
    # 1. Teach Mock DB to say "No Cycle Exists"
    mock_db.cycles.get.return_value = None
  
    # 2. Teach Mock Tradier to return a "junk" position (Short DTE)
    short_term_date = dt.date.today() + dt.timedelta(days=5)
  
    self.mock_t.account.get_positions.return_value = [{'symbol': 'SPY_JUNK', 'id': 123}]
    self.mock_t.quotes.get.return_value = {
      'expiration_date': short_term_date.strftime("%Y-%m-%d"),
      'greeks': {'delta': -0.25}
    }
  
    # 3. Run Function
    result = scan_and_initialize_cycle(self.mock_t, self.account_id)
  
    # 4. Verify we got None (Idle Mode)
    self.assertIsNone(result)
  
  @patch('server_helpers.get_quote') # <--- NEW PATCH
  @patch('server_helpers.app_tables')
  def test_valid_hedge_creates_full_trade_structure(self, mock_db, mock_get_quote): 
    """
      Scenario: Scanner finds a hedge.
      Expected: Creates Cycle -> Trade -> Transaction -> Leg (4 Linked Rows)
      """
    # 1. Setup Mock DB Returns
    mock_db.cycles.get.return_value = None

    # We need these to return Mock Objects so we can check their links later
    mock_cycle_row = MagicMock()
    mock_trade_row = MagicMock()
    mock_txn_row = MagicMock()

    mock_db.cycles.add_row.return_value = mock_cycle_row
    mock_db.trades.add_row.return_value = mock_trade_row
    mock_db.transactions.add_row.return_value = mock_txn_row

    # 2. Setup Mock Tradier
    self.mock_t.get_positions.return_value = [{
      'symbol': 'SPXW251219P05500000', 
      'id': 12345, 
      'quantity': 1.0, 
      'cost_basis': 250.0
    }]

    mock_get_quote.return_value = {
      'symbol': 'SPXW251219P05500000',
      'expiration_date': '2026-06-20', # Future date (>50 days)
      'greeks': {'delta': -0.25}       # Valid delta (abs(0.25))
    }
    # 3. Run Function
    scan_and_initialize_cycle(self.mock_t)

    # 4. Verify Hierarchy Creation
    # A. Cycle Created?
    mock_db.cycles.add_row.assert_called_once()

    # B. Trade Created? (Linked to Cycle?)
    mock_db.trades.add_row.assert_called_once()
    trade_args = mock_db.trades.add_row.call_args[1]
    self.assertEqual(trade_args['Strategy'], config.POSITION_TYPE_HEDGE)
    self.assertEqual(trade_args['Cycle'], mock_cycle_row) # Link Check

    # C. Transaction Created? (Linked to Trade?)
    mock_db.transactions.add_row.assert_called_once()
    txn_args = mock_db.transactions.add_row.call_args[1]
    self.assertEqual(txn_args['Trade'], mock_trade_row) # Link Check
    self.assertEqual(txn_args['TradierOrderID'], "12345")

    # D. Leg Created? (Linked to Transaction?)
    mock_db.legs.add_row.assert_called_once()
    leg_args = mock_db.legs.add_row.call_args[1]
    self.assertEqual(leg_args['Transaction'], mock_txn_row) # Link Check
    self.assertEqual(leg_args['OCCSymbol'], 'SPXW251219P05500000') # New Column Check

@anvil.server.callable
def skeleton_test_1():
  # 1. SETUP: Get a valid RuleSet ID (Assuming you have at least one row)
  # If this fails, add a row to your 'RuleSets' table first!
  rules = app_tables.rulesets.search()
  if not len(rules):
    print("ERROR: Please create at least one row in the 'RuleSets' table.")
  else:
    rule_id = rules[0].get_id()
    print(f"--- TEST START: Using RuleSet {rule_id} ---")
  
    # 2. CREATE
    print("\n1. Creating Cycle...")
    # Using dummy data for account/symbol
    cycle = start_new_cycle("TEST_ACCT", "SPX_TEST", rule_id)
    print(f"   Success! Created Cycle ID: {cycle.id}")
    print(f"   Status: {cycle.status}")
  
    # 3. VERIFY (Fetch via Dashboard)
    print("\n2. Fetching Dashboard...")
    dash_cycle = get_campaign_dashboard()
  
    if dash_cycle and dash_cycle.id == cycle.id:
      print("   Success! Dashboard returned the active cycle.")
    else:
      print(f"   FAILURE: Expected {cycle.id}, got {dash_cycle.id}")
  
      # 4. EXECUTE (Trigger Automation)
    print("\n3. Running Automation...")
    # Watch the console output for logs from server_main!
    result = run_auto()
    print(f"   Result: {result}")
  
    # 5. TEARDOWN (Close/Panic)
    print("\n4. Closing Campaign...")
    success = close_campaign_manual(cycle.id)
    print(f"   Closed Successfully: {success}")
  
    # 6. FINAL CHECK
    final_check = get_campaign_dashboard()
    if final_check is None:
      print("   Success! Cycle is closed and cleared from dashboard.")
    else:
      print(f"   Warning: Dashboard still returns: {final_check}")

@anvil.server.callable
def create_test_cycle_hierarchy():
  # 1. Get RuleSet (Corrected table name)
  rules = app_tables.rule_sets.get(name="Standard_0DTE") 
  if not rules:
    print("RuleSet 'Standard_0DTE' not found.")
    return

    # 2. Create Cycle
  cycle = app_tables.cycles.add_row(
    account="Test_Account_01",
    rule_set=rules,
    underlying="SPX",
    status="active",
    start_date=dt.date.today(),
    total_pnl=0.0,
    notes="Automated Test Cycle"
  )

  # 3. Create Trade
  trade = app_tables.trades.add_row(
    cycle=cycle,
    role="spread",
    status="open",
    entry_time=dt.datetime.now(),     # Timestamp
    exit_time=None,                # Timestamp
    quantity=1,
    entry_price=0.85, 
    capital_required=2500.0,
    target_harvest_price=0.40,
    roll_trigger_price=None,
    pnl=0.0,
    order_id_external="ORD_123_PARENT"
  )

  # 4. Create the OPENING Transaction
  open_trans = app_tables.transactions.add_row(
    trade=trade,
    action="STO",
    price=0.85,
    quantity=1,
    timestamp=dt.datetime.now(),
    fees=1.50,
    order_id_external="ORD_123_EXEC"
  )

  # 5. Create Legs (Linked to opening_transaction)
  # Note: closing_transaction is left None (Empty) because it's currently open
  app_tables.legs.add_row(
    trade=trade,
    opening_transaction=open_trans,  # <--- LINKED HERE
    closing_transaction=None,        # <--- EMPTY (Still Open)
    side=config.LEG_SIDE_SHORT,
    quantity=1,
    occ_symbol="SPXW230501P04000000",
    option_type="put",
    expiry=dt.date.today(),
    strike=4000,
    active=True,
    id_external="LEG_A_1"
  )

  app_tables.legs.add_row(
    trade=trade,
    opening_transaction=open_trans,  # <--- LINKED HERE
    closing_transaction=None,        # <--- EMPTY
    side=config.LEG_SIDE_LONG,
    quantity=1,
    occ_symbol="SPXW230501P03975000",
    option_type="put",
    expiry=dt.date.today(),
    strike=3975,
    active=True,
    id_external="LEG_B_1"
  )

  print(f"Test Hierarchy Created! Cycle ID: {cycle.get_id()}")

@anvil.server.callable
def run_scanner_tests():
  """
    Call this function to run the tests and see the report.
    """
  # Create a Test Suite
  suite = unittest.TestLoader().loadTestsFromTestCase(TestScanner)

  # Run the tests and capture output to a string buffer
  stream = StringIO()
  runner = unittest.TextTestRunner(stream=stream, verbosity=2)
  result = runner.run(suite)

  # Print the report to the Anvil logs
  print(stream.getvalue())

  return f"Tests Run: {result.testsRun}, Errors: {len(result.errors)}, Failures: {len(result.failures)}"

@anvil.server.callable
def test_entry_conditions() -> None:
  print("--- Unit Testing: Entry Conditions ---")

  # Mock Data
  mock_rules = {
    'trade_start_delay': 15,     # 15 minutes
    'gap_down_thresh': 0.02      # 2% limit
  }

  # Reference: Today at Market Open (09:30)
  # Note: Ensure shared.config.MARKET_OPEN_TIME is set to dt.time(9, 30)
  today = dt.date.today()
  base_open = dt.datetime.combine(today, dt.time(9, 30))

  # --- Test 1: Too Early (09:35) ---
  test_time = base_open + dt.timedelta(minutes=5)
  allowed, msg = server_libs.check_entry_conditions(
    current_price=4000.0, open_price=4000.0, previous_close=4000.0,
    current_time=test_time, rules=mock_rules
  )
  if not allowed and "Wait time" in msg:
    print("PASS: Early entry blocked.")
  else:
    print(f"FAIL: Early entry logic. Got: {allowed}, {msg}")

    # --- Test 2: Overnight Gap Down (-3%) ---
  test_time = base_open + dt.timedelta(minutes=30)
  allowed, msg = server_libs.check_entry_conditions(
    current_price=3880.0, open_price=3880.0, previous_close=4000.0,
    current_time=test_time, rules=mock_rules
  )
  if not allowed and "Overnight gap" in msg:
    print("PASS: Overnight gap blocked.")
  else:
    print(f"FAIL: Overnight gap logic. Got: {allowed}, {msg}")

    # --- Test 3: Intraday Crash (-3% since open) ---
  allowed, msg = server_libs.check_entry_conditions(
    current_price=3880.0, open_price=4000.0, previous_close=4000.0,
    current_time=test_time, rules=mock_rules
  )
  if not allowed and "Intraday drop" in msg:
    print("PASS: Intraday crash blocked.")
  else:
    print(f"FAIL: Intraday crash logic. Got: {allowed}, {msg}")

    # --- Test 4: Valid Entry ---
  allowed, msg = server_libs.check_entry_conditions(
    current_price=4005.0, open_price=4000.0, previous_close=4000.0,
    current_time=test_time, rules=mock_rules
  )
  if allowed:
    print("PASS: Valid entry accepted.")
  else:
    print(f"FAIL: Valid entry rejected. Got: {msg}")

  print("--- Test Complete ---")

@anvil.server.callable
def test_strike_selection() -> None:
  print("--- Unit Testing: Strike Selection ---")

  # Mock Option Chain (SPX-like structure)
  # Note: Puts have negative deltas
  mock_chain = [
    {'strike': 4000, 'delta': -0.50, 'option_type': 'put'},
    {'strike': 3975, 'delta': -0.30, 'option_type': 'put'},
    {'strike': 3950, 'delta': -0.20, 'option_type': 'put'}, # Target for Test 1
    {'strike': 3925, 'delta': -0.12, 'option_type': 'put'},
    {'strike': 3900, 'delta': -0.05, 'option_type': 'put'},

    # Call side just for verification
    {'strike': 4050, 'delta': 0.20, 'option_type': 'call'},
    {'strike': 4075, 'delta': 0.10, 'option_type': 'call'},
  ]

  # --- Test 1: Standard Put Spread (Target Delta 0.20, Width 25) ---
  # Expected: Short 3950 (-0.20 delta), Long 3925 (3950 - 25)
  result = server_libs.calculate_spread_strikes(
    chain=mock_chain,
    target_delta=0.20,
    spread_width=25,
    option_type="put"
  )

  if result == (3950, 3925):
    print(f"PASS: Standard Put Spread found {result}")
  else:
    print(f"FAIL: Standard Put Spread. Got {result}, Expected (3950, 3925)")

    # --- Test 2: Closest Delta Match (Target 0.10) ---
    # Expected: Short 3925 (-0.12) is closer to 0.10 than 3900 (-0.05)
    # Long: 3900 (3925 - 25)
  result = server_libs.calculate_spread_strikes(
    chain=mock_chain,
    target_delta=0.10,
    spread_width=25,
    option_type="put"
  )

  if result == (3925, 3900):
    print(f"PASS: Closest Delta logic found {result}")
  else:
    print(f"FAIL: Closest Delta logic. Got {result}, Expected (3925, 3900)")

    # --- Test 3: Missing Long Leg (Broken Wing) ---
    # Target 0.20 (Short 3950), Width 100 -> Target Long 3850 (Does not exist)
  result = server_libs.calculate_spread_strikes(
    chain=mock_chain,
    target_delta=0.20,
    spread_width=100, 
    option_type="put"
  )

  if result is None:
    print("PASS: Missing leg correctly returned None")
  else:
    print(f"FAIL: Missing leg validation. Got {result}")

  print("--- Test Complete ---")

@anvil.server.callable
def test_premium_and_sizing() -> None:
  print("--- Unit Testing: Premium & Sizing ---")

  # Mock Rules
  mock_rules = {
    'spread_min_premium': 0.50,
    'spread_max_premium': 2.50,
    'spread_size_factor': 5.0 
  }

  # Mock Legs for Premium Check
  # Short: 5.00, Long: 4.00 -> Credit: 1.00 (Valid)
  leg_short = {'bid': 4.90, 'ask': 5.10} # Mid 5.00
  leg_long =  {'bid': 3.90, 'ask': 4.10} # Mid 4.00

  # --- Test 1: Valid Premium ---
  valid, credit, msg = server_libs.validate_premium_and_size(leg_short, leg_long, mock_rules)
  if valid and credit == 1.0:
    print(f"PASS: Valid Premium accepted ({credit}).")
  else:
    print(f"FAIL: Valid Premium logic. Got {valid}, {credit}")

    # --- Test 2: Premium Too Low (< 0.50) ---
    # Short: 5.00, Long: 4.70 -> Credit: 0.30
  leg_long_expensive = {'bid': 4.60, 'ask': 4.80} # Mid 4.70
  valid, credit, msg = server_libs.validate_premium_and_size(leg_short, leg_long_expensive, mock_rules)
  if not valid and "below min" in msg:
    print(f"PASS: Low premium blocked ({credit}).")
  else:
    print(f"FAIL: Low premium logic. Got {valid}, {msg}")

    # --- Test 3: Sizing Logic ---
    # User Scenario: Hedge=2, Factor=5, Price=1.00 -> Expect 10
  qty = server_libs.get_spread_quantity(hedge_quantity=2, spread_price=1.00, rules=mock_rules)

  if qty == 10:
    print(f"PASS: Sizing calc correct (Qty: {qty}).")
  else:
    print(f"FAIL: Sizing calc. Expected 10, Got {qty}")

    # --- Test 4: Sizing with Higher Price ---
    # Hedge=1, Factor=5, Price=2.50 -> (1*5)/2.5 = 2
  qty = server_libs.get_spread_quantity(hedge_quantity=1, spread_price=2.50, rules=mock_rules)

  if qty == 2:
    print(f"PASS: High price sizing correct (Qty: {qty}).")
  else:
    print(f"FAIL: High price sizing. Expected 2, Got {qty}")

  print("--- Test Complete ---")


# --- MOCKS ---
class MockRow(dict):
  def get_id(self): return "123"

class MockCycle:
  def __init__(self, hedge_qty=None):
    self.hedge_trade = MockWrapper({'quantity': hedge_qty}) if hedge_qty else None

class MockWrapper:
  def __init__(self, row):
    self.quantity = row.get('quantity')

# --- TEST SUITE ---
class TestEntryLogic(unittest.TestCase):

  def setUp(self):
    # Match keys to what evaluate_entry expects
    self.rules = {
      'trade_start_delay': 15,
      'gap_down_thresh': 0.01,
      'spread_target_delta': 0.20,  # Matches your function's key
      'spread_width': 25,
      'spread_min_premium': 1.00,
      'spread_max_premium': 1.20,
      'spread_size_factor': 5.0
    }

    self.open_price = 5000.0
    self.prev_close = 5000.0
    self.valid_time = dt.datetime(2025, 1, 1, 9, 50, 0) 

    # Mock Chain
    self.chain = [
      {'strike': 4900, 'delta': -0.15, 'bid': 5.0, 'ask': 5.2, 'option_type': 'put'},
      {'strike': 4925, 'delta': -0.20, 'bid': 6.0, 'ask': 6.2, 'option_type': 'put'}, # Short
      {'strike': 4950, 'delta': -0.25, 'bid': 7.0, 'ask': 7.2, 'option_type': 'put'},

      {'strike': 4875, 'delta': -0.10, 'bid': 4.0, 'ask': 4.2, 'option_type': 'put'}, 
      {'strike': 4900, 'delta': -0.15, 'bid': 5.0, 'ask': 5.2, 'option_type': 'put'}, # Long
    ]

  def test_entry_success(self):
    """Test a perfect setup."""
    cycle = MockCycle(hedge_qty=1)

    # Call the function
    is_valid, data, msg = server_libs.evaluate_entry(
      cycle=cycle,
      current_time=self.valid_time,
      current_price=5000.0,
      open_price=self.open_price,
      previous_close=self.prev_close,
      option_chain=self.chain,
      rules=self.rules
    )

    self.assertTrue(is_valid)
    self.assertEqual(data['quantity'], 5)
    self.assertEqual(data['short_strike'], 4925)
    self.assertEqual(msg, "Entry Valid")

  def test_entry_fails_premium(self):
    """Test entry blocked if credit is too low."""
    # Short (6.1) - Long (5.6) = 0.50 Credit
    self.chain[4]['bid'] = 5.4 
    self.chain[4]['ask'] = 5.8 

    cycle = MockCycle(hedge_qty=1)
    is_valid, data, msg = server_libs.evaluate_entry(
      cycle, self.valid_time, 5000.0, 5000.0, 5000.0, self.chain, self.rules
    )

    self.assertFalse(is_valid)
    self.assertIn("below min", msg)

  def test_entry_fails_no_hedge(self):
    """Test entry blocked if no hedge exists."""
    cycle = MockCycle(hedge_qty=None)

    is_valid, data, msg = server_libs.evaluate_entry(
      cycle, self.valid_time, 5000.0, 5000.0, 5000.0, self.chain, self.rules
    )

    self.assertFalse(is_valid)
    self.assertIn("No active hedge", msg)

# Function to run in Anvil
@anvil.server.callable
def run_tests():
  suite = unittest.TestLoader().loadTestsFromTestCase(TestEntryLogic)
  runner = unittest.TextTestRunner(verbosity=2)
  runner.run(suite)


@anvil.server.callable
def run_api_tests():
  print("=== STARTING TRADIER API DIAGNOSTICS ===")

  # 1. Environment & Auth Check
  print("\n--- TEST 1: Environment & Auth ---")
  try:
    env_status = server_api.get_environment_status()
    print(f"Status: {env_status['status']}")
    print(f"Message: {env_status['status_message']}")
    print(f"Current Env: {env_status.get('current_env', 'UNKNOWN')}")
    print(f"Target Underlying: {env_status.get('target_underlying', 'UNKNOWN')}")

    if env_status['status_message'].startswith("API Error"):
      print("CRITICAL: Auth failed. Check API Keys.")
      return
  except Exception as e:
    print(f"CRITICAL: Connection Error: {e}")
    return

    # 2. Data Fetching Check (Real Chain)
  target = env_status.get('target_underlying')
  print(f"\n--- TEST 2: Fetching {target} Option Chain ---")

  # Use tomorrow/next week to ensure data exists
  test_date = dt.date.today() + dt.timedelta(days=7) 
  # Adjust to nearest Friday if needed, but usually 7 days out works for SPY/SPX

  chain = server_api.get_option_chain(date=test_date)

  if chain:
    print(f"SUCCESS: Received {len(chain)} options for {test_date}")
    print(f"Sample: {chain[0]['symbol']} | Bid: {chain[0]['bid']} | Delta: {chain[0].get('delta', 'N/A')}")

    # Pick 2 legs for the Order Test
    short_leg = chain[0] # Just grab first one
    long_leg = chain[1] if len(chain) > 1 else chain[0]
  else:
    print("FAILURE: Chain returned empty. (Is the market data subscription active for this env?)")
    # Try fetching underlying quote as fallback check
    print("Attempting to fetch underlying quote...")
    try:
      t = server_api._get_client()
      q = server_api._get_quote_direct(t, target)
      print(f"Quote Result: {q}")
    except:
      pass
    return

    # 3. Order Construction & Preview Check
  print("\n--- TEST 3: Order Logic & Preview ---")

  # Construct a dummy trade_data dict matching what 'evaluate_entry' produces
  dummy_trade = {
    'quantity': 1,
    'net_credit': 0.50,
    'short_leg_data': short_leg,
    'long_leg_data': long_leg
  }

  print(f"Testing open_spread_position with: {short_leg['symbol']} / {long_leg['symbol']}")
  try:
    result = server_api.open_spread_position(dummy_trade, preview=True)
    print("SUCCESS: Order Preview Accepted!")
    print(f"API Response: {result}")

  except Exception as e:
    print(f"CRITICAL: Order Preview Failed: {e}")

  print("\n=== DIAGNOSTICS COMPLETE ===")

@anvil.server.callable
def seed_spread_for_harvest():
  print("--- SEEDING HARVEST TEST (SMART & FIXED) ---")

  cycle_row = app_tables.cycles.get(status=config.STATUS_OPEN)
  if not cycle_row:
    print("Error: No Active Cycle found.")
    return

    # 1. Fetch REAL Chain
  target_date = dt.date.today() + dt.timedelta(days=10)
  chain = server_api.get_option_chain(date=target_date)

  # Fallback search
  if not chain:
    for d in range(5, 15):
      check_date = dt.date.today() + dt.timedelta(days=d)
      chain = server_api.get_option_chain(date=check_date)
      if chain: break

  if not chain or len(chain) < 2:
    print("CRITICAL: Could not find any valid option chain.")
    return

    # 2. Pick Legs
  puts = [o for o in chain if o.get('option_type') == 'put']
  if len(puts) < 2: return
  puts.sort(key=lambda x: x['strike'], reverse=True)
  short_leg = puts[0]
  long_leg = puts[1]

  print(f"Selected: {short_leg['symbol']} / {long_leg['symbol']}")

  # --- FIX: PARSE DATES ---
  def parse_api_date(val):
    if isinstance(val, str):
      return dt.datetime.strptime(val, "%Y-%m-%d").date()
    return val # Assume it's already a date if not string

    # Pre-calculate expiries
  short_expiry = parse_api_date(short_leg.get('expiration_date'))
  long_expiry = parse_api_date(long_leg.get('expiration_date'))
  # ------------------------

  # 3. Create Rows
  entry_credit = 0.31
  qty = 1

  trade_row = app_tables.trades.add_row(
    cycle=cycle_row,
    role=config.ROLE_INCOME,
    status=config.STATUS_OPEN,
    quantity=qty,
    entry_price=entry_credit,
    entry_time=dt.datetime.now(),
    pnl=0.0,
    order_id_external="SEED_SMART_HARVEST",
    target_harvest_price=entry_credit * 0.50,
    roll_trigger_price=entry_credit * 3.0,
    capital_required=qty * 100 * abs(short_leg['strike'] - long_leg['strike'])
  )

  txn = app_tables.transactions.add_row(
    trade=trade_row,
    action="OPEN_SPREAD",
    price=entry_credit,
    quantity=qty,
    timestamp=dt.datetime.now(),
    order_id_external="SEED_SMART_HARVEST"
  )

  app_tables.legs.add_row(
    trade=trade_row,
    opening_transaction=txn,
    side=config.LEG_SIDE_SHORT,
    quantity=qty,
    strike=short_leg['strike'],
    option_type='put',
    expiry=short_expiry, # <--- Uses Object
    occ_symbol=short_leg['symbol'],
    active=True
  )

  app_tables.legs.add_row(
    trade=trade_row,
    opening_transaction=txn,
    side=config.LEG_SIDE_LONG,
    quantity=qty,
    strike=long_leg['strike'],
    option_type='put',
    expiry=long_expiry, # <--- Uses Object
    occ_symbol=long_leg['symbol'],
    active=True
  )

  print("Seed Complete.")



def debug_live_greeks():
  print(f"--- DIAGNOSTIC: LIVE GREEKS ({config.ACTIVE_ENV}) ---")

  # 1. Get Context
  # We want today's chain (0DTE)
  # Note: Use server_api.get_environment_status()['today'] if strictly following bot logic,
  # but dt.date.today() is fine for a quick console check.
  target_date = dt.date.today()
  print(f"Fetching Chain for: {target_date}...")

  chain = server_api.get_option_chain(date=target_date)

  if not chain:
    print("ERROR: API returned no chain.")
    return

    # 2. Filter for Puts
  puts = [o for o in chain if o.get('option_type') == 'put']
  print(f"Received {len(puts)} Puts.")

  # 3. Sort by Strike (High to Low) for readability
  puts.sort(key=lambda x: x['strike'], reverse=True)

  print("\n--- STRIKE | DELTA REPORT (0.05 to 0.35 Range) ---")

  found_any = False
  for p in puts:
    strike = p.get('strike')

    # Extract Delta safely
    greeks = p.get('greeks')
    raw_delta = greeks.get('delta') if greeks else None

    # Only print if it's in the "Zone of Interest"
    # We handle None/0.0 explicitly to see if data is missing
    if raw_delta is None:
      print(f"Strike: {strike} | Delta: NONE (Missing Data)")
    elif raw_delta == 0:
      # Uncomment if you want to see the zeros (might be spammy)
      # print(f"Strike: {strike} | Delta: 0.0")
      pass
    else:
      delta = float(raw_delta)
      # Filter to relevant range for the strategy
      if -0.35 <= delta <= -0.05:
        print(f"Strike: {strike} | Delta: {delta}")
        found_any = True

  if not found_any:
    print("No Puts found with Deltas between -0.05 and -0.35.")

'''