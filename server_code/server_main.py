#import anvil.secrets
#import anvil.tables as tables
#import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

from shared import config
from shared.classes import Cycle
from . import server_libs  # The Brains (Clean Stubs)
from . import server_api  # The Hands (Dirty Stubs)
from . import server_db

@anvil.server.callable
@anvil.server.background_task
def run_automation_routine():
  print("LOG: Starting Automation Run...")

  # 1. LOAD CONTEXT (Dirty)
  # Fetch the one active campaign. 
  # Use server_db to fetch and FULLY HYDRATE the cycle (links trades, legs, hedge)
  cycle = server_db.get_active_cycle()

  if cycle:
    cycle_row = cycle._row
    print("In main loop:  cycle: \n" + "\n".join(f"{k} : {v}" for k, v in vars(cycle).items()))
  else:
    print("In main loop: No active cycle found (cycle is None)")

  # 2. PRECONDITIONS (Clean check of Dirty Data)
  # Get environment data (Time, Market Open/Close, Holiday)
  env_status = server_api.get_environment_status()

  if not server_libs.can_run_automation(env_status, cycle):
    print(f"LOG: Automation skipped. Reason: {env_status['status_message']}")
    return
    
  expected_symbol = env_status['target_underlying']
  if cycle and cycle.underlying != expected_symbol:
    print(f"WARNING: Cycle is {cycle.underlying} but Environment is {env_status['current_env']} ({expected_symbol}).")
    return
  

  # 3. SYNC REALITY (Dirty)
  # Ensure DB matches Tradier before making decisions
  positions = server_api.get_current_positions()

  if not server_libs.is_db_consistent(cycle, positions):
    # Stop everything if the map doesn't match the territory
    server_libs.alert_human("DB/Broker Mismatch Detected", level=config.ALERT_CRITICAL)
    return

    # 4. DETERMINE STATE (Clean)
    # The brain analyzes the cycle + market data and returns ONE state constant
  market_data = server_api.get_market_data_snapshot(cycle)
  decision_state = server_libs.determine_cycle_state(cycle, market_data)

  print(f"LOG: Decision State -> {decision_state}")

  # 5. EXECUTE (Dirty)
  if decision_state == config.STATE_PANIC_HARVEST:
    # Strategy: "Close everything immediately"
    server_api.close_all_positions(cycle)
    server_libs.alert_human("Panic Harvest Executed!", level=config.ALERT_CRITICAL)
    # Update DB
    if cycle_row:
      cycle_row['status'] = config.STATUS_CLOSED

  elif decision_state == config.STATE_ROLL_REQUIRED:
    # Strategy: Roll the income spread (Logic: Ask > 3x Credit)
    # 1. Get the specific trade that needs rolling
    spread_trade = server_libs.get_threatened_spread(cycle)
    # 2. Calculate new legs (e.g., 1 DTE, lower strike)
    new_legs_struct = server_libs.calculate_roll_legs(spread_trade, market_data)
    # 3. Execute
    server_api.execute_roll(spread_trade, new_legs_struct)

  elif decision_state == config.STATE_HARVEST_TARGET_HIT:
    # Strategy: Close spread at 50% profit
    spread_trade = server_libs.get_winning_spread(cycle)
    server_api.close_position(spread_trade)

  elif decision_state == config.STATE_HEDGE_MISSING:
    print("LOG: Hedge missing. Attempting to buy protection...")
    # Strategy: Buy the 90 DTE / 25 Delta put
    target_expiry = server_libs.get_target_hedge_date(cycle, env_status['today'])
    chain = server_api.get_option_chain(date=target_expiry)
    leg_to_buy = server_libs.select_hedge_strike(chain, target_delta=cycle.rule_set._row['hedge_target_delta'])
    if leg_to_buy:
      print(f"LOG: Selected Hedge: {leg_to_buy['symbol']} Delta: {leg_to_buy['greeks']['delta']}")

      # 4. Prepare Trade Data (Simulate a 'Trade Dict' for the recorder)
      # We construct this manually since we don't have an evaluate_hedge function yet
      trade_data = {
        'quantity': 1, # Always 1 unit per decision loop for now, or based on account size
        'short_strike': 0, # N/A
        'long_strike': leg_to_buy['strike'],
        'short_leg_data': {}, 
        'long_leg_data': leg_to_buy
      }
      order_res = server_api.buy_option(leg_to_buy)
      
      # 6. Record (DB)
      new_trade_obj = server_db.record_new_trade(
        cycle_row=cycle_row,
        role=config.ROLE_HEDGE,
        trade_dict=trade_data,
        order_id=order_res['id'],
        fill_price=order_res['price'], # Debit is positive price in this context? Or negative?
        fill_time=order_res['time']
      )
      cycle_row['hedge_trade'] = new_trade_obj._row
      print("LOG: Hedge executed, recorded, and linked to Cycle.")
    else:
      print("LOG: Could not find suitable hedge strike.")
      
  elif decision_state == config.STATE_SPREAD_MISSING:
    print("LOG: Attempting to enter new spread...")
    
    # Only if hedge is present (checked inside determine_cycle_state)
    chain = server_api.get_option_chain(date=env_status['today'])
    
    # 2. Evaluate Entry
    is_valid, trade_data, reason = server_libs.evaluate_entry(
      cycle=cycle,
      current_time=env_status['now'],
      current_price=market_data['price'],
      open_price=market_data['open'],
      previous_close=market_data['previous_close'],
      option_chain=chain,
      rules=cycle.rule_set._row # Pass the raw dictionary from the wrapper
    )
    if is_valid:
      print(f"LOG: Entry Valid! Qty: {trade_data['quantity']} Credit: {trade_data['net_credit']}")

      # 3. Execute Order (API)
      order_res = server_api.open_spread_position(trade_data)

      # 4. Record to DB (Persistence)
      # Note: We import server_db at the top of main
      server_db.record_new_trade(
        cycle_row=cycle_row, # The raw row
        role=config.ROLE_INCOME,
        trade_dict=trade_data,
        order_id=order_res['id'],
        fill_price=order_res['price'],
        fill_time=order_res['time']
      )
      print("LOG: Trade recorded successfully.")

    else:
      print(f"LOG: Entry Logic Rejected: {reason}")

  elif decision_state == config.STATE_HEDGE_ADJUSTMENT_NEEDED:
    # Strategy: Roll to fresh 90 DTE / 25 Delta Put
    print("LOG: Executing Hedge Reset...")

    # 1. Identify the old hedge
    old_hedge_trade = cycle.hedge_trade_link

    # 2. Find the new target (90 days out, 25 delta)
    target_expiry = server_libs.get_target_hedge_date()
    chain = server_api.get_option_chain(target_expiry)
    new_leg_struct = server_libs.select_hedge_strike(chain)

    # 3. Execute the Roll (Close Old, Open New)
    server_api.execute_roll(old_hedge_trade, new_leg_struct)

    # 4. Update the Cycle's DailyHedgeRef to the new price?
    # Note: We likely need to reset the reference price since we changed the instrument
    # cycle.daily_hedge_ref = new_leg_struct['price'] (To be implemented)

  elif decision_state == config.STATE_IDLE:
    print("LOG: No action required.")

  else:
    print(f"LOG: Unhandled State: {decision_state}")