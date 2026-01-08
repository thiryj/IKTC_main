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
  current_env_account = config.ACTIVE_ENV # e.g., 'PROD' or 'SANDBOX'
  print(f"LOG: Starting Automation Run on environment: {current_env_account}")
  
  # 1. GLOBAL PRECONDITIONS
  # Check environment status (Market Open/Closed) and kill switch false BEFORE touching DB
  env_status = server_api.get_environment_status()

  # Fetch global settings (Singleton row)
  # Assuming 'settings' table has exactly one row
  settings_row = app_tables.settings.get() 
  system_settings = dict(settings_row) if settings_row else {}
  
  # This check is now purely for "Is the Market Open?" / "Is Bot Enabled globally?"
  if not server_libs.can_run_automation(env_status, system_settings):
    print(f"LOG: Automation skipped. Market: {env_status.get('status')} | Enabled: {system_settings['automation_enabled']}")
    return
    
  # 2. LOAD CONTEXT (or auto seed)
  cycle = server_db.get_active_cycle(current_env_account)
  if not cycle:
    print("LOG: System Idle - No Active Cycle found. Seeding empty cycle for autmoation to populate.")
    rules = app_tables.rule_sets.get(name=config.ACTIVE_RULESET)
    if not rules:
      print(f"Error: RuleSet {config.ACTIVE_RULESET} not found.")
      return
    symbol = config.TARGET_UNDERLYING[server_api.CURRENT_ENV]

    app_tables.cycles.add_row(
      account=current_env_account,
      underlying=symbol,
      status=config.STATUS_OPEN,
      start_date=dt.date.today(),
      total_pnl=0.0,
      daily_hedge_ref=0.0, # Will be set when spread opens
      rule_set=rules,
      notes="Seeded Empty Cycle"
    )

    cycle = server_db.get_active_cycle(current_env_account)
    print(f"Cycle {cycle.id} created and hydrated. Proceeding immediately.")
  cycle_row = cycle._row
  #print("In main loop:  cycle: \n" + "\n".join(f"{k} : {v}" for k, v in vars(cycle).items()))
    
  expected_symbol = env_status['target_underlying']
  if cycle and cycle.underlying != expected_symbol:
    print(f"WARNING: Cycle is {cycle.underlying} but Environment is {env_status['current_env']} ({expected_symbol}).")
    return
  
  # 3. SYNC REALITY (Dirty)
  # Ensure DB matches Tradier before making decisions
  positions = server_api.get_current_positions()

  # check for positions open in db that are not in broker
  zombies = server_libs.get_zombie_trades(cycle, positions)
  if config.ENFORCE_ZOMBIE_CHECKS and zombies:
    print(f"LOG: Found {len(zombies)} Zombie Trades (Open in DB, Missing in Broker). Marking as worst case loss. Must edit db to broker reality")
    for z_trade in zombies:
      try:
        server_db.settle_zombie_trade(z_trade._row)
        msg = f"Zombie Trade {z_trade.id} detected and settled at MAX LOSS. Manual Check Required."
        print(f"CRITICAL: {msg}")
        server_libs.alert_human(msg, level=config.ALERT_CRITICAL)
      except Exception as e:
        print(f"CRITICAL: Failed to settle Zombie Trade {z_trade.id}: {e}")

      # The in-memory 'cycle' object still thinks those trades are OPEN.
      # We must re-fetch from DB to get the clean state before running logic.
      print("LOG: Re-loading Cycle Context after Zombie Settlement...")
      cycle = server_db.get_active_cycle(config.ACTIVE_ENV)
      if not cycle:
        print("LOG: Cycle closed during settlement? Stopping run.")
        return

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
        order_res = server_api.close_position(trade, order_type='market')
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

    spread_trade = server_libs.get_threatened_spread(cycle, market_data)
    if not spread_trade:
      print("LOG: Error - State is ROLL but no threatened trade found.")
      return

    # --- STEP 1: CLOSE LIABILITY (Market Order) ---
    print(f"LOG: Step 1 - Emergency Closing Trade {spread_trade.id}...")

    # Force Market Order for immediate exit
    close_res = server_api.close_position(spread_trade, order_type='market')
    close_order_id = close_res.get('id')

    if not close_order_id:
      print("CRITICAL: Roll Aborted - API rejected Close Order.")
      return

    # Poll for Fill (Aggressive Wait)
    is_closed = server_api.wait_for_order_fill(close_order_id, timeout_seconds=10)

    if not is_closed:
      print("CRITICAL: Roll Aborted - Close Order timed out/failed.")
      # Note: Position is likely stuck in 'pending' state. Bot will retry next loop.
      return

    # DB Update (Close Old)
    # We use the estimated price from response if specific fill price isn't available yet
    # Ideally, we'd fetch the exact fill price here, but let's use the snapshot/response for speed
    realized_debit = close_res['price'] 

    server_db.close_trade(
      trade_row=spread_trade._row,
      fill_price=realized_debit, 
      fill_time=dt.datetime.now(),
      order_id=close_order_id
    )
    print(f"LOG: Liability Closed. Realized Debit: ${realized_debit:.2f}")

    # --- STEP 2: RE-ENTRY LOGIC ---

    # A. Check Safety (Don't re-enter if market is crashing)
    is_safe, safety_msg = server_libs.check_roll_safety(market_data, cycle.rules)
    if not is_safe:
      print(f"LOG: Roll Re-Entry Aborted: {safety_msg}. Staying Flat.")
      return

    # B. Find Target Date (1-2 DTE)
    valid_dates = server_api.get_expirations()
    # Target 1 day out (Tomorrow)
    target_date = server_libs.find_closest_expiration(valid_dates, target_dte=1)
    if not target_date:
      print("LOG: Roll Re-Entry Failed: No valid expiration found.")
      return

    print(f"LOG: Targeting Re-Entry for {target_date}...")
    chain = server_api.get_option_chain(date=target_date)
    if not chain:
      print("LOG: Roll Re-Entry Failed: Chain empty.")
      return

    # C. Calculate New Legs
    # We need to cover the 'realized_debit' we just paid
    current_short = next(l for l in spread_trade.legs if l.side == config.LEG_SIDE_SHORT)

    roll_result = server_libs.calculate_roll_legs(
      chain=chain,
      current_short_strike=current_short.strike,
      width=cycle.rules['spread_width'],
      cost_to_close=realized_debit
    )
    if not roll_result:
      print("LOG: Roll Re-Entry Failed: No valid strikes found to cover cost (Scratch impossible). Staying Flat.")
      return

    # --- STEP 3: OPEN ASSET (Limit Order) ---
    print(f"LOG: Step 2 - Opening New Spread (Limit ${roll_result['new_credit']:.2f})...")

    # Construct Trade Data for API
    # Note: calculate_roll_legs returns 'new_credit' which is the GROSS credit of new spread
    trade_data = {
      'quantity': spread_trade.quantity, # Maintain same size
      'short_strike': roll_result['short_leg']['strike'],
      'long_strike': roll_result['long_leg']['strike'],
      'short_leg_data': roll_result['short_leg'],
      'long_leg_data': roll_result['long_leg'],
      'net_credit': roll_result['new_credit']
    }

    open_res = server_api.open_spread_position(trade_data)
    open_order_id = open_res.get('id')
    if open_order_id:
      # Wait for Fill (IOC Simulation)
      
      is_opened = server_api.wait_for_order_fill(open_order_id, config.ORDER_TIMEOUT_SECONDS)
      if is_opened:
        # Reset daily_hedge_ref
        current_hedge = market_data.get('hedge_last', 0.0)
        if current_hedge > 0:
          cycle.daily_hedge_ref = current_hedge
          cycle._row['daily_hedge_ref'] = current_hedge
          print(f"LOG: Roll Complete. Hedge Reference reset to ${current_hedge:.2f}")
        server_db.record_new_trade(
          cycle_row=cycle_row,
          role=config.ROLE_INCOME,
          trade_dict=trade_data,
          order_id=open_order_id,
          fill_price=roll_result['new_credit'],
          fill_time=dt.datetime.now()
        )
        print("LOG: Roll Re-Entry Successful.")
      else:
        print("LOG: Re-Entry timed out. Canceling...")
        server_api.cancel_order(open_order_id)
        print("LOG: Order canceled. System Flat (Stop Loss Taken).")
    else:
      print("CRITICAL: API rejected Re-Entry Order.")

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
    # 1. Get Valid Expirations
    valid_dates = server_api.get_expirations()
    target_dte = cycle.rules['hedge_target_dte'] # e.g. 90
    #target_expiry = server_libs.get_target_hedge_date(cycle, env_status['today'])
    
    # 2. Pick Best Date
    best_date = server_libs.find_closest_expiration(valid_dates, target_dte)
    if not best_date:
      print("CRITICAL: API returned NO valid expirations.")
      return
    print(f"LOG: Target {target_dte} DTE. Selected Expiry: {best_date}")
    
    # 3. Fetch Chain & Select Strike (Standard logic follows...)
    chain = server_api.get_option_chain(date=best_date)
    leg_to_buy = server_libs.select_hedge_strike(chain, 
                                                 target_delta=cycle.rules['hedge_target_delta'])
    
    if leg_to_buy:
      print(f"LOG: Selected Hedge: {leg_to_buy['symbol']}")
      _execute_hedge_entry(cycle, leg_to_buy)
    else:
      print("LOG: Chain found, but no suitable strike (Delta match) found.")
      
  elif decision_state == config.STATE_SPREAD_MISSING:
    print("LOG: Attempting to enter new spread...")
    
    chain = server_api.get_option_chain(date=env_status['today'])
    
    # 2. Evaluate Entry
    is_valid, trade_data, reason = server_libs.evaluate_entry(
      cycle=cycle,
      chain=chain,
      market_data=market_data,
      env_status=env_status,
      rules=cycle.rules # Pass the raw dictionary from the wrapper
    )
    if is_valid:
      print(f"LOG: Entry Valid! Qty: {trade_data['quantity']} Credit: {trade_data['net_credit']}")

      # We lock in the hedge price NOW to measure relative performance for Panic Logic
      current_hedge_price = market_data.get('hedge_last', 0.0)
      if current_hedge_price > 0:
        cycle.daily_hedge_ref = current_hedge_price
        # Persist to DB
        cycle_row['daily_hedge_ref'] = current_hedge_price
        print(f"LOG: Hedge Reference set to ${current_hedge_price:.2f}")
      else:
        print("WARNING: Hedge price is 0. Panic Logic might be inaccurate.")
        
      # 3. Execute Order (API)
      order_res = server_api.open_spread_position(trade_data)
      order_id = order_res.get('id')
      if not order_id:
        print("CRITICAL: Entry order rejected by broker.")
      else:
        # 2. SYNCHRONOUS WAIT (The IOC Simulation)
        print(f"LOG: Waiting {config.ORDER_TIMEOUT_SECONDS}s for fill...")
        is_filled = server_api.wait_for_order_fill(order_id, config.ORDER_TIMEOUT_SECONDS)

        if is_filled:
          # 3A. SUCCESS: Record to DB

          # Capture Hedge Ref NOW since we are officially in the trade
          # We use the snapshot value from earlier, or could re-fetch if strict
          current_hedge = market_data.get('hedge_last', 0.0)
          if current_hedge > 0:
            cycle.daily_hedge_ref = current_hedge
            cycle._row['daily_hedge_ref'] = current_hedge
            print(f"LOG: Hedge Reference set to ${current_hedge:.2f}")

          server_db.record_new_trade(
            cycle_row=cycle_row, 
            role=config.ROLE_INCOME,
            trade_dict=trade_data,
            order_id=order_id,
            fill_price=order_res['price'],
            fill_time=dt.datetime.now() # Use actual time
          )
          print("LOG: Trade filled and recorded.")

        else:
          # 3B. TIMEOUT: Cancel and Abort
          print("LOG: Entry timed out. Canceling order...")
          server_api.cancel_order(order_id)
          print("LOG: Order canceled. System remains IDLE.")
    else:
      print(reason)
      
  elif decision_state == config.STATE_HEDGE_ADJUSTMENT_NEEDED:
    print("LOG: Hedge Adjustment Required. Rolling position...")

    old_hedge = cycle.hedge_trade_link

    # 1. Close Old Hedge
    print(f"LOG: Closing old hedge {old_hedge.id}...")
    close_res = server_api.close_position(old_hedge)
    close_order_id = close_res.get('id')
    if close_order_id:
      is_closed = server_api.wait_for_order_fill(close_order_id, timeout_seconds=10)
      if is_closed:
        server_db.close_trade(
          trade_row=old_hedge._row,
          fill_price=close_res['price'],
          fill_time=dt.datetime.now(), # Use actual time, or close_res['time'] if available/parsed
          order_id=close_order_id
        )
        print("LOG: Old Hedge Closed successfully.")
      else:
        print("CRITICAL: Hedge Close timed out. Aborting Roll to prevent double-hedging or nakedness.")
        # We abort here because if we can't close the old one, we shouldn't buy a new one 
        return
    else:
      print("CRITICAL: API rejected Hedge Close order. Aborting Roll.")
      return

    # 2. Buy New Hedge
    # 1. Get Valid Expirations
    valid_dates = server_api.get_expirations()
    target_dte = cycle.rules['hedge_target_dte'] # e.g. 90

    # 2. Pick Best Date
    best_date = server_libs.find_closest_expiration(valid_dates, target_dte)
    if not best_date:
      print("CRITICAL: API returned NO valid expirations.")
      return
    print(f"LOG: Target {target_dte} DTE. Selected Expiry: {best_date}")
    
    chain = server_api.get_option_chain(date=best_date)
    if not chain:
      print("CRITICAL: Closed old hedge but could not find ANY new chain to enter.")
      # Optional: Alert human here
      return

    # Select Strike
    leg_to_buy = server_libs.select_hedge_strike(
      chain, 
      target_delta=cycle.rules['hedge_target_delta']
    )
    if leg_to_buy:
      success = _execute_hedge_entry(cycle, leg_to_buy)
      if success:
        print("LOG: Hedge Roll Complete.")
      else:
        print("LOG: Hedge Roll Failed (Entry canceled). Cycle is currently Unhedged.")
    else:
      print("CRITICAL: Closed old hedge but could not find new one!")
      
  elif decision_state == config.STATE_IDLE:
    print("LOG: No action required.")

  else:
    print(f"LOG: Unhandled State: {decision_state}")

def _execute_hedge_entry(cycle, leg_to_buy) -> bool:
  """
    Helper to execute, verify, and record a Hedge Entry.
    Returns True if successful, False if failed/canceled.
    """
  print(f"LOG: Buying new hedge: {leg_to_buy['symbol']}")

  # 1. Execute
  buy_res = server_api.buy_option(leg_to_buy)
  order_id = buy_res.get('id')

  if not order_id:
    print("CRITICAL: Hedge Order rejected by API.")
    return False

    # 2. Verify Fill
  print("LOG: Waiting for Hedge Fill confirmation...")
  is_filled = server_api.wait_for_order_fill(order_id, timeout_seconds=10)

  if is_filled:
    # 3. Record
    trade_data = {
      'quantity': 1,
      'short_strike': 0,
      'long_strike': leg_to_buy['strike'],
      'short_leg_data': {}, 
      'long_leg_data': leg_to_buy
    }

    new_trade_obj = server_db.record_new_trade(
      cycle_row=cycle._row, # Access internal Anvil Row
      role=config.ROLE_HEDGE,
      trade_dict=trade_data,
      order_id=order_id,
      fill_price=buy_res['price'],
      fill_time=dt.datetime.now()
    )

    # 4. Link
    cycle._row['hedge_trade'] = new_trade_obj._row
    print("LOG: Hedge executed, verified, and linked.")
    return True

  else:
    # 5. Fail/Cancel
    print("CRITICAL: Hedge Market Order timed out/stuck. Canceling...")
    server_api.cancel_order(order_id)
    print("LOG: Hedge Order Canceled.")
    return False