import anvil.server
import unittest
from unittest.mock import MagicMock, patch
import datetime
from io import StringIO

# IMPORTANT: Adjust this import if your function is in a different module
from server_helpers import scan_and_initialize_cycle 

class TestScanner(unittest.TestCase):

  def setUp(self):
    # This runs automatically BEFORE every single test.
    # It creates a fresh "Fake Tradier" so one test doesn't mess up another.
    self.mock_t = MagicMock()
    self.account_id = "TEST_ACCT_123"

    # Create a date 60 days in the future for testing valid DTE
    self.future_date = datetime.date.today() + datetime.timedelta(days=60)
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
    short_term_date = datetime.date.today() + datetime.timedelta(days=5)
  
    self.mock_t.account.get_positions.return_value = [{'symbol': 'SPY_JUNK', 'id': 123}]
    self.mock_t.quotes.get.return_value = {
      'expiration_date': short_term_date.strftime("%Y-%m-%d"),
      'greeks': {'delta': -0.25}
    }
  
    # 3. Run Function
    result = scan_and_initialize_cycle(self.mock_t, self.account_id)
  
    # 4. Verify we got None (Idle Mode)
    self.assertIsNone(result)
  
  @patch('server_helpers.app_tables')
  def test_valid_hedge_creates_cycle(self, mock_db):
    # 1. No Cycle Exists
    mock_db.cycles.get.return_value = None
  
    # 2. Tradier returns a "Perfect Hedge"
    self.mock_t.account.get_positions.return_value = [{'symbol': 'SPY_PERFECT', 'id': 999}]
    self.mock_t.quotes.get.return_value = {
      'expiration_date': self.future_date_str, # 60 Days out
      'greeks': {'delta': -0.25}               # Perfect Delta
    }
  
    # 3. Teach Mock DB to return a "Success" object when add_row is called
    new_row_mock = {'ID': 'new_cycle_row'}
    mock_db.cycles.add_row.return_value = new_row_mock
  
    # 4. Run Function
    result = scan_and_initialize_cycle(self.mock_t, self.account_id)
  
    # 5. Verify the result
    self.assertEqual(result, new_row_mock)
  
    # CRITICAL: Verify the function actually tried to save to the DB
    mock_db.cycles.add_row.assert_called_once()
  
    # Verify it saved the right data (Status='OPEN', etc.)
    args = mock_db.cycles.add_row.call_args[1]
    self.assertEqual(args['Status'], 'OPEN')
    self.assertEqual(args['HedgeSymbol'], 'SPY_PERFECT')

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