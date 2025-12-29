import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

from shared import config
from shared.classes import Cycle
from . import server_libs  # The Brains (Clean Stubs)
from . import tradier_api  # The Hands (Dirty Stubs)

@anvil.server.callable
@anvil.server.background_task
def run_automation_routine():
  print("LOG: Starting Automation Run...")

  # 1. LOAD CONTEXT (Dirty)
  # Fetch the one active campaign. 
  # Logic: There should only be one 'OPEN' cycle at a time.
  cycle_row = app_tables.cycles.get(Status=config.STATUS_OPEN)

  # Hydrate the Cycle object (or None if we are starting fresh)
  # Note: If no cycle exists, we create a dummy one or handle it in logic
  cycle = Cycle(cycle_row) if cycle_row else None
  print("In main loop:  cycle: \n" + "\n".join(f"{k} : {v}" for k, v in vars(cycle).items()))

  # 2. PRECONDITIONS (Clean check of Dirty Data)
  # Get environment data (Time, Market Open/Close, Holiday)
  env = tradier_api.get_environment_status()

  if not server_libs.can_run_automation(env, cycle):
    print(f"LOG: Automation skipped. Reason: {env['status_message']}")
    return

  # 3. SYNC REALITY (Dirty)
  # Ensure DB matches Tradier before making decisions
  positions = tradier_api.get_current_positions()

  if not server_libs.is_db_consistent(cycle, positions):
    # Stop everything if the map doesn't match the territory
    server_libs.alert_human("DB/Broker Mismatch Detected", level=config.ALERT_CRITICAL)
    return

    # 4. DETERMINE STATE (Clean)
    # The brain analyzes the cycle + market data and returns ONE state constant
  market_data = tradier_api.get_market_data_snapshot(cycle)
  decision_state = server_libs.determine_cycle_state(cycle, market_data)

  print(f"LOG: Decision State -> {decision_state}")

  # 5. EXECUTE (Dirty)
  if decision_state == config.STATE_PANIC_HARVEST:
    # Strategy: "Close everything immediately"
    tradier_api.close_all_positions(cycle)
    server_libs.alert_human("Panic Harvest Executed!", level=config.ALERT_CRITICAL)
    # Update DB
    if cycle_row:
      cycle_row['Status'] = config.STATUS_CLOSED

  elif decision_state == config.STATE_ROLL_REQUIRED:
    # Strategy: Roll the income spread (Logic: Ask > 3x Credit)
    # 1. Get the specific trade that needs rolling
    spread_trade = server_libs.get_threatened_spread(cycle)
    # 2. Calculate new legs (e.g., 1 DTE, lower strike)
    new_legs_struct = server_libs.calculate_roll_legs(spread_trade, market_data)
    # 3. Execute
    tradier_api.execute_roll(spread_trade, new_legs_struct)

  elif decision_state == config.STATE_HARVEST_TARGET_HIT:
    # Strategy: Close spread at 50% profit
    spread_trade = server_libs.get_winning_spread(cycle)
    tradier_api.close_position(spread_trade)

  elif decision_state == config.STATE_HEDGE_MISSING:
    # Strategy: Buy the 90 DTE / 25 Delta put
    target_expiry = server_libs.get_target_hedge_date()
    chain = tradier_api.get_option_chain(target_expiry)
    leg_to_buy = server_libs.select_hedge_strike(chain)
    tradier_api.buy_option(leg_to_buy)

  elif decision_state == config.STATE_SPREAD_MISSING:
    # Strategy: Sell the 0DTE spread
    # Only if hedge is present (checked inside determine_cycle_state)
    chain = tradier_api.get_option_chain(date=env['today'])
    legs_to_sell = server_libs.select_spread_strikes(chain)
    tradier_api.open_spread_position(legs_to_sell)

  elif decision_state == config.STATE_HEDGE_ADJUSTMENT_NEEDED:
    # Strategy: Roll to fresh 90 DTE / 25 Delta Put
    print("LOG: Executing Hedge Reset...")

    # 1. Identify the old hedge
    old_hedge_trade = cycle.hedge_trade_link

    # 2. Find the new target (90 days out, 25 delta)
    target_expiry = server_libs.get_target_hedge_date()
    chain = tradier_api.get_option_chain(target_expiry)
    new_leg_struct = server_libs.select_hedge_strike(chain)

    # 3. Execute the Roll (Close Old, Open New)
    tradier_api.execute_roll(old_hedge_trade, new_leg_struct)

    # 4. Update the Cycle's DailyHedgeRef to the new price?
    # Note: We likely need to reset the reference price since we changed the instrument
    # cycle.daily_hedge_ref = new_leg_struct['price'] (To be implemented)

  elif decision_state == config.STATE_IDLE:
    print("LOG: No action required.")

  else:
    print(f"LOG: Unhandled State: {decision_state}")