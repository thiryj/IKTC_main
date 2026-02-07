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

  print("--- BATCH API TEST ---")
  print(f"Duration: {duration:.2f} seconds")
  print(f"Underlying: {snapshot.get('price')}")
  print(f"Hedge Last: {snapshot.get('hedge_last')}")
  print(f"Spread Marks: {snapshot.get('spread_marks')}")

  return {
    "duration": duration,
    "underlying": snapshot.get('price'),
    "marks_count": len(snapshot.get('spread_marks', {}))
  }
  
@anvil.server.callable
def run_branch_test(scenario: str) -> str:
  """
    Forces the bot into a specific state branch for testing.
    Scenarios: 'PANIC', 'ROLL_SPREAD', 'WINDFALL', 'HARVEST', 'HEDGE_ROLL'
    """
  #if not config.DRY_RUN:
  #  return "ABORTED: You must set config.DRY_RUN = True before running branch tests!"

  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
  if not cycle: 
    server_main._execute_automation_loop()
    cycle = server_db.get_active_cycle(config.ACTIVE_ENV)

  if not cycle: 
    return "ERROR: Could not seed campaign. Check RuleSet table."

  # 1. SETUP MOCK DATA
  # We create a fake market_data snapshot to 'trick' the logic
  #mock_data = server_api.get_market_data_snapshot(cycle)
  env_status = server_api.get_environment_status()

  if scenario == 'SCALPEL_ENTRY':
    env_status['now'] = dt.datetime.combine(dt.date.today(), dt.time(15, 5))

    # MOCK DATA: 0.15% above VWAP
    mock_vwap_pct = 0.0015 
    mock_env = {
      'vix': 18.0,
      'vwap': 6800.0,
      'price': 6810.2, # Resulting in +0.0015
      'vwap_pct': mock_vwap_pct,
      'is_bullish': True
    }

    print(f"TEST: Mocking Entry at +{mock_vwap_pct*100:.3f}% from VWAP.")

    # This will trigger the pass-through chain
    server_main.process_scalpel_entry_logic(cycle, mock_env, env_status, is_dry_run=True)

    return "Scalpel Entry Test Executed. Check Trades table for 'vwap_pct' and 'entry_bias'."

  elif scenario == 'WIRING_TEST':
    # 1. Manually construct a 'Candidate' dictionary
    # This bypasses the search engine and real API data
    mock_candidate = {
      'long_leg_data': {'symbol': 'SPXW_FAKE_L', 'strike': 5000, 'bid': 5.0, 'ask': 5.2},
      'short_leg_data': {'symbol': 'SPXW_FAKE_S', 'strike': 5005, 'bid': 3.8, 'ask': 4.0},
      'debit': 1.25,
      'quantity': 1,
      'short_strike': 5005,
      'long_strike': 5000
    }

    mock_vwap_pct = 0.0025 # +0.25%
    bias = 'CALL'

    print(f"TEST: Forcing execution sync with Bias: {bias} and VWAP%: {mock_vwap_pct}")

    # 2. Call the execution helper directly
    # This tests if _execute_scalpel_entry -> _execute_entry_and_sync -> record_new_trade is working
    from . import server_main
    success = server_main._execute_scalpel_entry(
      cycle=cycle,
      candidate=mock_candidate,
      is_dry_run=True,
      entry_bias=bias,
      vwap_pct=mock_vwap_pct
    )

    return f"Wiring Test {'SUCCESS' if success else 'FAILED'}. Check Trades table."

  elif scenario == 'SCALPEL_WIN':
    # 1. Ensure an open trade exists with a DRY_ order ID
    # 2. MOCK THE FILL: Set time to 3:30 PM
    cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
    env_status['now'] = dt.datetime.combine(dt.date.today(), dt.time(15, 30))

    # We don't need mock environment data because the bot 
    # is just checking wait_for_order_fill.

    # 3. TRIGGER BRANCH
    decision = server_libs.determine_scalpel_state(cycle, env_status)
    print(f"TEST: State -> {decision}")

    if decision == config.STATE_ACTIVE_HUNT:
      # This will call wait_for_order_fill
      # It will auto-fill in DRY_RUN and bank the $3.50
      server_main.process_state_decision(cycle, decision, {}, env_status)
      

@anvil.server.callable
def launch_purge() -> str:
  """Entry point to start the purge in the background."""
  anvil.server.launch_background_task('purge_garbage_background')
  return "Purge process started in background. Check App Logs for progress."

@anvil.server.background_task
def purge_garbage_background() -> None:
  import datetime as dt
  from anvil.tables import app_tables
  import anvil.tables.query as q
  from . import server_logging as logger

  # 1. Define the Cutoff (Midnight on Jan 21, 2026)
  cutoff_date = dt.datetime(2026, 1, 21)
  print(f"BKG: Starting Purge for data older than {cutoff_date}...")

  # 2. Identify the 'Garbage' Trades
  bad_trades = list(app_tables.trades.search(entry_time=q.less_than(cutoff_date)))
  trade_count = len(bad_trades)

  if trade_count == 0:
    print("BKG: No garbage data found.")
    return

    # 3. Cascading Delete (Looping through and committing every 10 rows for speed)
  for i, t in enumerate(bad_trades):
    # Delete children
    for l in app_tables.legs.search(trade=t): l.delete()
    for txn in app_tables.transactions.search(trade=t): txn.delete()
      # Delete parent
    t.delete()

    # Log progress every 10 trades to the Console/App Logs
    if i % 10 == 0:
      print(f"BKG: Processed {i}/{trade_count} trades...")

    # 4. Cleanup Cycles
  print("BKG: Cleaning up Cycle rows...")
  all_cycles = app_tables.cycles.search()
  for c in all_cycles:
    remaining_trades = list(app_tables.trades.search(cycle=c))
    if len(remaining_trades) == 0:
      c.delete()
    else:
      # Sync PnL for legitimate campaigns
      total = sum([(t['pnl'] or 0) * (t['quantity'] or 0) * 100 
                   for t in app_tables.trades.search(cycle=c, status='CLOSED')])
      c['total_pnl'] = round(total, 2)

  print("BKG: Purge Complete.")

@anvil.server.callable
def scrub_orphaned_rows() -> str:
  l_count = 0
  t_count = 0

  # 1. Scrub Legs
  for row in app_tables.legs.search():
    try:
      # Attempt to read a property on the linked trade
      # If the trade is deleted, this will throw an error
      if row['trade'] is not None:
        _ = row['trade']['role'] 
    except:
      row.delete()
      l_count += 1

    # 2. Scrub Transactions
  for row in app_tables.transactions.search():
    try:
      if row['trade'] is not None:
        _ = row['trade']['role']
    except:
      row.delete()
      t_count += 1

  return f"Deep Scrub Complete: Removed {l_count} orphan Legs and {t_count} orphan Transactions."

@anvil.server.callable
def diagnostic_settings_sync() -> str:
  print("--- DIAGNOSTIC: SETTINGS SYNC TEST ---")

  # 1. TEST: GET
  original_settings = anvil.server.call('get_live_settings')
  if not isinstance(original_settings, dict):
    return "FAILURE: get_live_settings did not return a dictionary."

  orig_val = original_settings.get('ui_refresh_seconds', 60)
  print(f"Original UI Refresh: {orig_val}")

  # 2. TEST: SET (Update a value temporarily)
  test_val = orig_val + 5
  success = anvil.server.call('save_live_settings', {'ui_refresh_seconds': test_val})

  if not success:
    return "FAILURE: save_live_settings returned False."

    # 3. TEST: VERIFY
  updated_settings = anvil.server.call('get_live_settings')
  new_val = updated_settings.get('ui_refresh_seconds')
  print(f"Updated UI Refresh: {new_val}")

  # CLEANUP: Revert to original
  anvil.server.call('save_live_settings', {'ui_refresh_seconds': orig_val})

  if new_val == test_val:
    return f"SUCCESS: Settings sync verified. (Test Value: {test_val})"
  else:
    return f"FAILURE: Data mismatch. Sent {test_val}, got {new_val}"