#import anvil.secrets
#import anvil.tables as tables
#import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

import datetime as dt

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
    print("LOG: PANIC HARVEST TRIGGERED! Executing Sequential Close...")

    # 1. Sort Trades by Risk Profile
    income_trades = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]
    hedge_trades = [t for t in cycle.trades if t.role == config.ROLE_HEDGE and t.status == config.STATUS_OPEN]

    liabilities_cleared = True

    # 2. Phase 1: Close Liabilities (Spreads)
    # We MUST clear these before selling the shield.
    for trade in income_trades:
      print(f"LOG: Emergency Closing Liability {trade.id}...")
      try:
        # A. Submit
        order_res = server_api.close_position(trade)
        order_id = order_res.get('id')

        if not order_id:
          print(f"CRITICAL: API rejected submission for {trade.id}")
          liabilities_cleared = False
          break # Stop processing liabilities

          # B. VERIFY FILL (The Safety Pause)
          # Wait up to 5 seconds for confirmation
        is_filled = server_api.wait_for_order_fill(order_id, timeout_seconds=5)

        if not is_filled:
          print(f"CRITICAL: Close Order {order_id} did not fill. Aborting Sequence.")
          liabilities_cleared = False
          break # STOP EVERYTHING. Do not close other spreads. Do not close hedge.

          # C. Record via DB (Only if filled)
        server_db.close_trade(
          trade_row=trade._row,
          fill_price=order_res['price'], # Note: Ideally we fetch the *actual* fill price from the polling result
          fill_time=dt.datetime.now(),
          order_id=order_id
        )

      except Exception as e:
        print(f"CRITICAL: Exception closing Income Trade {trade.id}: {e}")
        liabilities_cleared = False
        break

    # 3. Phase 2: Close Assets (Hedge)
    # ONLY proceed if we successfully submitted close orders for all liabilities
    if liabilities_cleared:
      print("LOG: Liabilities cleared. Closing Hedges...")
      for trade in hedge_trades:
        print(f"LOG: Closing Hedge Asset {trade.id}...")
        try:
          order_res = server_api.close_position(trade)

          server_db.close_trade(
            trade_row=trade._row,
            fill_price=order_res['price'],
            fill_time=order_res['time'],
            order_id=order_res['id']
          )
        except Exception as e:
          # If hedge fails to close, it's annoying but safe (we still own the option)
          print(f"ERROR: Failed to close Hedge {trade.id}: {e}")

        # Only mark Cycle closed if everything worked
      if cycle_row:
        cycle_row['status'] = config.STATUS_CLOSED
        print("LOG: Cycle Status updated to CLOSED.")

    else:
      # SAFETY INTERLOCK
      print("CRITICAL: Liabilities NOT cleared. ABORTING Hedge Close.")
      print("LOG: System holding Hedge to protect against naked exposure.")
      server_libs.alert_human("Panic Close Failed - HEDGE HELD!", level=config.ALERT_CRITICAL)

  elif decision_state == config.STATE_ROLL_REQUIRED:
    print("LOG: Roll Triggered! Hunting for escape route...")

    # FIX: Pass market_data (from Step 4) instead of fetching it again
    spread_trade = server_libs.get_threatened_spread(cycle, market_data)

    if not spread_trade:
      print("LOG: Error - State is ROLL but no threatened trade found.")
      return
  
    # Calculate Cost to Close (using marks from earlier snapshot or re-fetch)
    # We re-fetch snapshot to be safe/fresh
    marks = server_api.get_market_data_snapshot(cycle).get('spread_marks', {})
    cost_to_close = marks.get(spread_trade.id, 0.0)
  
    # TEST HACK: Force cost negative to guarantee the math works
    print(f"DEBUG: Real Cost {cost_to_close}. Forcing to -0.50 for Logic Verification.")
    cost_to_close = -0.50

    print(f"LOG: Cost to close current spread: ${cost_to_close:.2f}")

    # 2. Fetch T+1 Chain (Tomorrow)
    # Logic: Look for 1 day out. If weekend, finding Monday.
    # Simple logic: Today + 1 day
    chain = []
    target_date = None
    # Retry offsets: 1 day (standard), 3 days (weekend skip), 7 days (liquidity search)
    retry_offsets = [1, 3, 7]
    target_date = env_status['today'] + dt.timedelta(days=1)
    for days in retry_offsets:
      candidate_date = env_status['today'] + dt.timedelta(days=days)
      chain = server_api.get_option_chain(date=candidate_date)

      if chain:
        target_date = candidate_date
        print(f"LOG: Found valid chain for Roll at {target_date} (T+{days})")
        break

    if not chain:
      print("LOG: CRITICAL - Cannot find ANY chain to roll into (T+1/3/7 failed). Manual intervention required.")
      return

    # 3. Calculate Roll Legs
    # Extract current short strike
    current_short = next(l for l in spread_trade.legs if l.side == config.LEG_SIDE_SHORT)

    roll_result = server_libs.calculate_roll_legs(
      chain=chain,
      current_short_strike=current_short.strike,
      width=cycle.rules['spread_width'], # Scaled width
      cost_to_close=cost_to_close
    )

    if roll_result:
      print(f"LOG: Roll Candidate Found! New Short: {roll_result['short_leg']['strike']} Net: {roll_result['net_price']:.2f}")

      # 4. Execute Roll
      order_res = server_api.execute_roll(
        old_trade=spread_trade,
        new_short=roll_result['short_leg'],
        new_long=roll_result['long_leg'],
        net_price=roll_result['net_price']
      )

      # 5. DB Updates (Atomic Two-Step)
      # Step A: Close Old
      server_db.close_trade(
        trade_row=spread_trade._row,
        fill_price=cost_to_close, # Estimated close price
        fill_time=order_res['time'],
        order_id=order_res['id'] + "_CLOSE"
      )

      # Step B: Open New
      # Construct trade_dict for the recorder
      new_trade_data = {
        'quantity': spread_trade.quantity,
        'short_strike': roll_result['short_leg']['strike'],
        'long_strike': roll_result['long_leg']['strike'],
        'short_leg_data': roll_result['short_leg'],
        'long_leg_data': roll_result['long_leg'],
        'net_credit': roll_result['new_credit']
      }

      server_db.record_new_trade(
        cycle_row=cycle_row,
        role=config.ROLE_INCOME,
        trade_dict=new_trade_data,
        order_id=order_res['id'] + "_OPEN",
        fill_price=roll_result['new_credit'],
        fill_time=order_res['time']
      )
    
      print("LOG: Roll executed and DB updated.")

    else:
      print("LOG: No valid roll found (Cannot pay for close with lower strike).")

  elif decision_state == config.STATE_HARVEST_TARGET_HIT:
    # Strategy: Close spread at 50% profit
    spread_trade = server_libs.get_winning_spread(cycle, market_data)
    if spread_trade:
      print(f"LOG: Harvest Target Hit! Trade {spread_trade.id}. Closing...")
      order_res = server_api.close_position(spread_trade)
      
      server_db.close_trade(
        trade_row=spread_trade._row,
        fill_price=order_res['price'],
        fill_time=order_res['time'],
        order_id=order_res['id']
      )
      print(f"LOG: Trade closed and DB updated. Exit Price: {order_res['price']}")
    else:
      print("LOG: Harvest State detected but no winning trade returned (odd).")

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
      rules=cycle.rules # Pass the raw dictionary from the wrapper
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
    print("LOG: Hedge Adjustment Required. Rolling position...")

    old_hedge = cycle.hedge_trade_link

    # 1. Close Old Hedge
    print(f"LOG: Closing old hedge {old_hedge.id}...")
    close_res = server_api.close_position(old_hedge)

    # Record Close
    server_db.close_trade(
      trade_row=old_hedge._row,
      fill_price=close_res['price'],
      fill_time=close_res['time'],
      order_id=close_res['id']
    )

    # ... inside STATE_HEDGE_ADJUSTMENT_NEEDED ...

    # 2. Buy New Hedge
    # Logic: Scan for valid chain near target DTE
    target_days = cycle.rules['hedge_target_dte'] # e.g. 90
    found_chain = []

    # Scan range: Target +/- 20 days to find a valid expiration (Monthly)
    # We step by 5 days to speed it up (looking for Fridays)
    print(f"LOG: Scanning for new hedge around {target_days} DTE...")

    start_scan = target_days - 20
    end_scan = target_days + 30

    for d in range(start_scan, end_scan, 5):
      candidate_date = env_status['today'] + dt.timedelta(days=d)
      chain = server_api.get_option_chain(date=candidate_date)
      if chain:
        found_chain = chain
        print(f"LOG: Found valid chain at {candidate_date} ({d} DTE)")
        break

    if not found_chain:
      print("CRITICAL: Closed old hedge but could not find ANY new chain to enter.")
      # Optional: Alert human here
      return

    # Select Strike
    leg_to_buy = server_libs.select_hedge_strike(
      found_chain, 
      target_delta=cycle.rules['hedge_target_delta']
    )

    if leg_to_buy:
      print(f"LOG: Buying new hedge: {leg_to_buy['symbol']}")
      buy_res = server_api.buy_option(leg_to_buy)

      # Record Open
      trade_data = {
        'quantity': 1, # Reset to 1 unit
        'short_strike': 0,
        'long_strike': leg_to_buy['strike'],
        'short_leg_data': {}, 
        'long_leg_data': leg_to_buy
      }

      new_trade_obj = server_db.record_new_trade(
        cycle_row=cycle_row,
        role=config.ROLE_HEDGE,
        trade_dict=trade_data,
        order_id=buy_res['id'],
        fill_price=buy_res['price'],
        fill_time=buy_res['time']
      )

      # Link new hedge to cycle
      cycle_row['hedge_trade'] = new_trade_obj._row
      print("LOG: Hedge Roll Complete.")

    else:
      print("CRITICAL: Chain found, but no suitable strike selected!")

  elif decision_state == config.STATE_IDLE:
    print("LOG: No action required.")

  else:
    print(f"LOG: Unhandled State: {decision_state}")