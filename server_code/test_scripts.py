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
  
@anvil.server.callable
def run_branch_test(scenario: str) -> str:
  """
    Forces the bot into a specific state branch for testing.
    Scenarios: 'PANIC', 'ROLL_SPREAD', 'WINDFALL', 'HARVEST', 'HEDGE_ROLL'
    """
  if not config.DRY_RUN:
    return "ABORTED: You must set config.DRY_RUN = True before running branch tests!"

  cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
  if not cycle: return "No active cycle found."

  # 1. SETUP MOCK DATA
  # We create a fake market_data snapshot to 'trick' the logic
  mock_data = server_api.get_market_data_snapshot(cycle)
  env_status = server_api.get_environment_status()

  if scenario == 'PANIC':
    # Force a massive drop: Open 5000, Price 4500
    mock_data['open'] = 5000.0
    mock_data['price'] = 4500.0
    mock_data['hedge_last'] = 150.0 

  elif scenario == 'ROLL_SPREAD':
    # Find an open spread and set its 'Mark' to be > Trigger Price (e.g. 5.00)
    income_trade = next((t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN), None)

    if income_trade:
      # 2. Force the 'Mark' (Current Cost) to be ABOVE the trigger
      # If trigger is 3.00, we'll make it 3.50
      mock_data['spread_marks'][income_trade.id] = 3.50
      print(f"TEST: Mocking Spread {income_trade.id} cost at $3.50 (Trigger: {income_trade.roll_trigger_price})")
    else:
      return "ERROR: You need one OPEN Income Spread in the DB to test a Roll."

  elif scenario == 'WINDFALL':
    # Hedge Profit > 10x Theta
    mock_data['spread_marks'] = {} # Naked
    hedge = cycle.hedge_trade_link
    entry_px = hedge.entry_price or 10.0

    # We need Profit > (Factor * abs(Theta))
    # If Factor is 10 and Theta is 0.60, we need > $6.00 profit
    mock_data['hedge_last'] = entry_px + 15.0 # Guaranteed trigger
    mock_data['hedge_theta'] = 0.60

    print(f"TEST: Mocking Hedge at ${mock_data['hedge_last']} (Entry: ${entry_px}, Trigger: >${mock_data['hedge_theta']*10})")

  elif scenario == 'HEDGE_ROLL':
    # Hedge Profit > 10x Theta
    mock_data['hedge_dte'] = 45
    print('testing hedge roll with old hedge')
    pass
  elif scenario == 'WINDFALL':
    # Condition: No spreads open + Hedge Profit > 10x Theta
    mock_data['spread_marks'] = {} # Force naked
    mock_data['hedge_last'] = 200.0 # Force high price
    mock_data['hedge_theta'] = 1.0  # 10x1 = 10. Profit 200-Entry > 10.
    print(f"TEST: Mocking Naked Hedge at ${mock_data['hedge_last']} (Windfall Target)")

  elif scenario == 'HARVEST':
    # Condition: Spread Cost <= Target Harvest Price
    income_trade = next((t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN), None)
    if income_trade:
      # If target is 0.50, mock the cost at 0.40
      target = income_trade.target_harvest_price or 1.16
      mock_data['spread_marks'][income_trade.id] = target - 0.10
      print(f"TEST: Mocking Spread {income_trade.id} cost at ${mock_data['spread_marks'][income_trade.id]} (Target: {target})")
    else:
      return "ERROR: Need an OPEN Income Spread to test Harvest."

  elif scenario == 'SPREAD_MISSING':
    # Condition: No open income spreads + valid market time
    mock_data['spread_marks'] = {} # Ensure no open spreads
    # Ensure we are 'past' the trade_start_delay (e.g., mock current time to 10:00 AM)
    hedge = cycle.hedge_trade_link
    if hedge and hedge.entry_price:
      mock_data['hedge_last'] = hedge.entry_price
    else:
      mock_data['hedge_last'] = 0.01

    mock_data['hedge_theta'] = 1.0 # Keep threshold at 10.0
      # This makes _has_traded_today look for trades on a date with no data.
    mock_today = dt.date.today() + dt.timedelta(days=1)

    env_status['today'] = mock_today
    env_status['now'] = dt.datetime.combine(dt.date.today(), dt.time(10, 0))
    print("TEST: Mocking 'Flat' state at 10:00 AM to trigger Entry.")

  elif scenario == 'RECOVERY_HUNT':
    # 1. Ensure we are flat
    mock_data['spread_marks'] = {}

    # 2. Mock 'Calm' Market conditions so strikes are found
    mock_data['price'] = 6900.0
    mock_data['open'] = 6900.0
    mock_data['previous_close'] = 6900.0

    # 3. Ensure we are in the trading window
    env_status['now'] = dt.datetime.combine(dt.date.today(), dt.time(11, 0))

    print("TEST: Mocking 'Flat' state with a recent Roll Exit to trigger Recovery Hunt.")
    

  # 2. RUN ORCHESTRATOR
  # Instead of the heartbeat, we call the specific decision + execution logic
  
  decision = server_libs.determine_cycle_state(cycle, mock_data, env_status)
  print(f"TEST: Scenario '{scenario}' resulted in State: {decision}")

  # 3. TRIGGER EXECUTION
  # This will run your new abstracted _execute_settlement_and_sync logic
  try:
    server_main.process_state_decision(cycle, decision, mock_data, env_status)
    return f"Branch Test {scenario} ({decision}) EXECUTED. Check logs."
  except Exception as e:
    return f"Execution Error in {decision}: {e}"

# In server_utils.py or server_db.py

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