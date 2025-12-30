import anvil.server
import unittest
from unittest.mock import MagicMock, patch
import datetime as dt
from io import StringIO

# IMPORTANT: Adjust this import if your function is in a different module
from server_helpers import scan_and_initialize_cycle 
from shared import config

from . server_client import start_new_cycle, get_campaign_dashboard, run_auto, close_campaign_manual
from anvil.tables import app_tables

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
