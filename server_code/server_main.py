import anvil.email
import anvil.server
from anvil.tables import app_tables

import datetime as dt
import pytz

from shared import config
from shared.classes import Cycle
from . import server_libs  # The Brains (Clean Stubs)
from . import server_api  # The Hands (Dirty Stubs)
from . import server_db, server_logging as logger

@anvil.server.callable
@anvil.server.background_task
def run_automation_routine():    
  logger.log("Starting Automation Run ...",
    level=config.LOG_INFO,
    source=config.LOG_SOURCE_ORCHESTRATOR)
  
  current_env_account = config.ACTIVE_ENV # e.g., 'PROD' or 'SANDBOX'
  # 1. GLOBAL PRECONDITIONS
  # Check environment status (Market Open/Closed) and kill switch false BEFORE touching DB
  env_status = server_api.get_environment_status()

  # Fetch global settings (Singleton row)
  # Assuming 'settings' table has exactly one row
  settings_row = app_tables.settings.get() 
  system_settings = dict(settings_row) if settings_row else {}
  
  # This check is now purely for "Is the Market Open?" / "Is Bot Enabled globally?"
  if not server_libs.can_run_automation(env_status, system_settings):
    logger.log(f"Automation skipped. Market: {env_status.get('status_message')}", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return
    
  # 2. LOAD CONTEXT (or auto seed)
  cycle = server_db.get_active_cycle(current_env_account)
  if not cycle:
    logger.log("System Idle - No Active Cycle found. Seeding empty cycle...", 
               level=config.LOG_WARNING, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    rules = app_tables.rule_sets.get(name=config.ACTIVE_RULESET)
    if not rules:
      logger.log(f"CRITICAL ERROR: RuleSet '{config.ACTIVE_RULESET}' not found. Cannot auto-seed.", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return
    symbol = config.TARGET_UNDERLYING[current_env_account]

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
    logger.log(f"Cycle {cycle.id} created and hydrated. Proceeding immediately.", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
  cycle_row = cycle._row
    
  # 3. SYNC REALITY (Dirty)
  # Ensure DB matches Tradier before making decisions
  positions = server_api.get_current_positions()

  # check for positions open in db that are not in broker
  zombies = server_libs.get_zombie_trades(cycle, positions)
  if config.ENFORCE_ZOMBIE_CHECKS and zombies:
    logger.log(f"Found {len(zombies)} Zombie Trades. Executing Fail-Safe Settlement...", 
               level=config.LOG_WARNING, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    for z_trade in zombies:
      try:
        server_db.settle_zombie_trade(z_trade._row)
        msg = f"Zombie Trade {z_trade.id} detected and settled at MAX LOSS. Manual Check Required."
        logger.log(msg, 
                   level=config.LOG_CRITICAL, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
      except Exception as e:
        logger.log(f"Failed to settle Zombie Trade {z_trade.id}: {e}", 
                   level=config.LOG_CRITICAL, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)

      # RELOAD CONTEXT
      logger.log("Re-loading Cycle Context after Zombie Settlement...", 
                 level=config.LOG_INFO, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      cycle = server_db.get_active_cycle(current_env_account)
      if not cycle:
        return

  if not server_libs.is_db_consistent(cycle, positions):
    # Stop everything if the map doesn't match the territory
    logger.log("DB/Broker Mismatch Detected (Non-Critical). Proceeding...", 
               level=config.LOG_CRITICAL, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return

    # 4. DETERMINE STATE
    # The brain analyzes the cycle + market data and returns ONE state constant
  market_data = server_api.get_market_data_snapshot(cycle)
  decision_state = server_libs.determine_cycle_state(cycle, market_data)

  logger.log(f"Decision State -> {decision_state}", 
             level=config.LOG_INFO, 
             source=config.LOG_SOURCE_ORCHESTRATOR, 
             context={'cycle_id': cycle.id})

  # 5. EXECUTE
#---------------------------------------------------#  
  if decision_state == config.STATE_PANIC_HARVEST:
    logger.log("PANIC HARVEST TRIGGERED! Executing Sequential Close...", 
               level=config.LOG_CRITICAL, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

    # 1. Sort Trades by Risk Profile
    income_trades = [t for t in cycle.trades if t.role == config.ROLE_INCOME and t.status == config.STATUS_OPEN]
    hedge_trades = [t for t in cycle.trades if t.role == config.ROLE_HEDGE and t.status == config.STATUS_OPEN]

    liabilities_cleared = True

    # 2. Phase 1: Close Liabilities (Spreads)
    # We MUST clear these before selling the shield.
    for trade in income_trades:
      logger.log(f"Emergency Closing Liability {trade.id}...", 
                 level=config.LOG_INFO, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      try:
        # A. Submit
        order_res = server_api.close_position(trade, order_type='market')
        order_id = order_res.get('id')
        if not order_id:
          logger.log(f"API rejected submission for {trade.id}", 
                     level=config.LOG_CRITICAL, 
                     source=config.LOG_SOURCE_ORCHESTRATOR)
          liabilities_cleared = False
          continue # Best effort: Try closing other liabilities

        # Poll for fill
        is_filled, fill_px = server_api.wait_for_order_fill(order_id, timeout_seconds=5)
        if not is_filled:
          logger.log(f"Close Order {order_id} did not fill. Aborting Sequence.", 
                     level=config.LOG_CRITICAL, 
                     source=config.LOG_SOURCE_ORCHESTRATOR)
          liabilities_cleared = False
          continue # Note: We do NOT cancel market orders in panic; we hope they fill eventually.

        final_price = fill_px if fill_px > 0 else float(order_res['price'])
        
        server_db.close_trade(
          trade_row=trade._row,
          fill_price=final_price, 
          fill_time=dt.datetime.now(),
          order_id=order_id
        )

      except Exception as e:
        logger.log(f"Exception closing Income Trade {trade.id}: {e}", 
                   level=config.LOG_CRITICAL, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
        liabilities_cleared = False
        
    # 3. Phase 2: Close Assets (Hedge)
    # ONLY proceed if we successfully submitted close orders for all liabilities
    if liabilities_cleared:
      logger.log("Liabilities cleared. Closing Hedges...", 
                 level=config.LOG_INFO, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      for trade in hedge_trades:
        logger.log(f"Closing Hedge Asset {trade.id}...", 
                   level=config.LOG_INFO, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
        try:
          order_res = server_api.close_position(trade, order_type='market')
          order_id = order_res.get('id')
          if order_id:
            is_filled, fill_px = server_api.wait_for_order_fill(order_id, timeout_seconds=5)
            if is_filled:
              final_price = fill_px if fill_px > 0 else float(order_res['price'])
              server_db.close_trade(
                trade_row=trade._row,
                fill_price=final_price,
                fill_time=order_res['time'],
                order_id=order_res['id']
              )
            else:
              logger.log(f"Hedge Close {order_id} timed out. Manual check required.", 
                         level=config.LOG_CRITICAL, 
                         source=config.LOG_SOURCE_ORCHESTRATOR)
          else:
            logger.log(f"API rejected Hedge Close for {trade.id}", 
                       level=config.LOG_CRITICAL, 
                       source=config.LOG_SOURCE_ORCHESTRATOR)
        except Exception as e:
          # If hedge fails to close, it's annoying but safe (we still own the option)
          logger.log(f"Failed to close Hedge {trade.id}: {e}", 
                     level=config.LOG_WARNING, 
                     source=config.LOG_SOURCE_ORCHESTRATOR)

        # Only mark Cycle closed if everything worked
      if cycle_row:
        cycle_row['status'] = config.STATUS_CLOSED
        logger.log("Cycle Status updated to CLOSED.", 
                   level=config.LOG_INFO, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)

    else:
      logger.log("Liabilities NOT cleared. ABORTING Hedge Close. System holding Hedge.", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
#---------------------------------------------------#
  elif decision_state == config.STATE_ROLL_REQUIRED:
    logger.log("Roll Triggered! Initiating Split Roll Sequence...", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

    spread_trade = server_libs.get_threatened_spread(cycle, market_data)
    if not spread_trade: return

    # --- STEP 1: CLOSE LIABILITY (Market Order) ---
    logger.log(f"Step 1 - Emergency Closing Trade {spread_trade.id}...", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

    # Force Market Order for immediate exit
    close_res = server_api.close_position(spread_trade, order_type='market')
    close_order_id = close_res.get('id')
    if not close_order_id:
      logger.log("Roll Aborted - API rejected Close Order.", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return

    # Poll for Fill (Aggressive Wait)
    is_closed, close_px = server_api.wait_for_order_fill(close_order_id, timeout_seconds=10)
    if not is_closed:
      logger.log("Roll Aborted - Close Order timed out/failed.", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      # Note: Position is likely stuck in 'pending' state. Bot will retry next loop.
      return

    realized_debit = close_px if close_px > 0 else float(close_res['price'])

    server_db.close_trade(
      trade_row=spread_trade._row,
      fill_price=realized_debit, 
      fill_time=dt.datetime.now(),
      order_id=close_order_id
    )
    logger.log(f"Liability Closed. Realized Debit: ${realized_debit:.2f}", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

    # --- STEP 2: RE-ENTRY LOGIC ---

    # A. Check Safety (Don't re-enter if market is crashing)
    is_safe, safety_msg = server_libs.check_roll_safety(market_data, cycle.rules)
    if not is_safe:
      logger.log(f"Roll Re-Entry Aborted: {safety_msg}. Staying Flat.", 
                 level=config.LOG_WARNING, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return

    # B. Find Target Date (1-2 DTE)
    valid_dates = server_api.get_expirations()
    target_date = server_libs.find_closest_expiration(valid_dates, target_dte=1)
    if not target_date:
      logger.log("Roll Re-Entry Failed: No valid expiration found.", 
                 level=config.LOG_WARNING, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return

    chain = server_api.get_option_chain(date=target_date)
    if not chain:
      logger.log("Roll Re-Entry Failed: Chain empty.", 
                 level=config.LOG_WARNING, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return

    # C. Calculate New Legs
    # We need to cover the 'realized_debit' we just paid
    current_short = next(l for l in spread_trade.legs if l.side == config.LEG_SIDE_SHORT)
    if not current_short: 
      logger.log("Roll Re-Entry Failed: Error retrieving closing short strike. Staying Flat.", 
                 level=config.LOG_WARNING, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return
    roll_result = server_libs.calculate_roll_legs(
      chain=chain,
      current_short_strike=current_short.strike,
      width=cycle.rules['spread_width'],
      cost_to_close=realized_debit
    )
    if not roll_result:
      logger.log("Roll Re-Entry Failed: No valid strikes found to cover cost. Staying Flat.", 
                 level=config.LOG_WARNING, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return

    # --- STEP 3: OPEN ASSET (Limit Order) ---
    logger.log(f"Step 2 - Opening New Spread (Limit ${roll_result['new_credit']:.2f})...", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

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
      logger.log(f"Waiting for Re-Entry fill ({config.ORDER_TIMEOUT_SECONDS}s)...", 
                 level=config.LOG_INFO, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      is_opened, open_px = server_api.wait_for_order_fill(open_order_id, config.ORDER_TIMEOUT_SECONDS)
      if is_opened:
        final_credit = open_px if open_px > 0 else roll_result['new_credit']
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
          fill_price=final_credit,
          fill_time=dt.datetime.now()
        )
        logger.log("Roll Re-Entry Successful.", 
                   level=config.LOG_INFO, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
      else:
        logger.log("Re-Entry timed out. Canceling...", 
                   level=config.LOG_WARNING, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
        
        if server_api.cancel_order(open_order_id):
          logger.log("Order canceled. System Flat (Stop Loss Taken).", 
                    level=config.LOG_INFO, 
                    source=config.LOG_SOURCE_ORCHESTRATOR)
        else:
          logger.log(f"CRITICAL: Failed to cancel stuck Roll Entry {open_order_id}!", 
                     level=config.LOG_CRITICAL, 
                     source=config.LOG_SOURCE_ORCHESTRATOR)
    else:
      logger.log("API rejected Re-Entry Order.", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
#---------------------------------------------------#
  elif decision_state == config.STATE_HARVEST_TARGET_HIT:
    # Strategy: Close spread at 50% profit
    spread_trade = server_libs.get_winning_spread(cycle, market_data)
    if spread_trade:
      logger.log(f"Harvest Target Hit! Trade {spread_trade.id}. Closing...", 
                 level=config.LOG_INFO, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      order_res = server_api.close_position(spread_trade)    
      order_id = order_res.get('id')
      if order_id:
        is_filled, fill_px = server_api.wait_for_order_fill(order_id, config.ORDER_TIMEOUT_SECONDS)
        if is_filled:
          final_price = fill_px if fill_px > 0 else float(order_res['price'])
          server_db.close_trade(
            trade_row=spread_trade._row,
            fill_price=final_price,
            fill_time=order_res['time'],
            order_id=order_res['id']
          )
          logger.log("Trade closed and DB updated.", 
                    level=config.LOG_INFO, 
                    source=config.LOG_SOURCE_ORCHESTRATOR)
        else:
          logger.log("Harvest timed out. Canceling...", 
                     level=config.LOG_WARNING, 
                     source=config.LOG_SOURCE_ORCHESTRATOR)
          if server_api.cancel_order(order_id):
            logger.log("Harvest order canceled. Will retry next cycle.", 
                       level=config.LOG_INFO, 
                       source=config.LOG_SOURCE_ORCHESTRATOR)
          else:
            logger.log(f"CRITICAL: Failed to cancel stuck Harvest Order {order_id}!", 
                       level=config.LOG_CRITICAL, 
                       source=config.LOG_SOURCE_ORCHESTRATOR)
#---------------------------------------------------#
  elif decision_state == config.STATE_HEDGE_MISSING:
    logger.log("Hedge missing. Attempting to buy protection...", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    # Strategy: Buy the 90 DTE / 25 Delta put
    # 1. Get Valid Expirations
    valid_dates = server_api.get_expirations()
    target_dte = cycle.rules['hedge_target_dte'] # e.g. 90
        
    best_date = server_libs.find_closest_expiration(valid_dates, target_dte)
    if not best_date:
      logger.log("New hedge needed but API returned NO valid expirations.", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return
    logger.log(f"Target {target_dte} DTE. Selected Expiry: {best_date}", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    
    # 3. Fetch Chain & Select Strike (Standard logic follows...)
    chain = server_api.get_option_chain(date=best_date)
    leg_to_buy = server_libs.select_hedge_strike(chain, 
                                                 target_delta=cycle.rules['hedge_target_delta'])
    if leg_to_buy:
      _execute_hedge_entry(cycle, leg_to_buy)
    else:
      logger.log("Chain found, but no suitable strike (Delta match) found.", 
                 level=config.LOG_WARNING, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
#---------------------------------------------------#      
  elif decision_state == config.STATE_SPREAD_MISSING:
    logger.log("Attempting to enter new spread...", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    
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
      logger.log(f"Entry Valid! Qty: {trade_data['quantity']}", 
                 level=config.LOG_INFO, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
        
      # 3. Execute Order (API)
      order_res = server_api.open_spread_position(trade_data)
      order_id = order_res.get('id')
      if not order_id:
        logger.log("New spread entry order rejected by broker.", 
                   level=config.LOG_CRITICAL, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
      else:
        # 2. SYNCHRONOUS WAIT (The IOC Simulation)
        is_filled, fill_px = server_api.wait_for_order_fill(order_id, config.ORDER_TIMEOUT_SECONDS)
        if is_filled:
          final_price = fill_px if fill_px > 0 else float(order_res['price'])
          current_hedge = market_data.get('hedge_last', 0.0)
          if current_hedge > 0:
            cycle.daily_hedge_ref = current_hedge
            cycle._row['daily_hedge_ref'] = current_hedge
          
          server_db.record_new_trade(
            cycle_row=cycle_row, 
            role=config.ROLE_INCOME,
            trade_dict=trade_data,
            order_id=order_id,
            fill_price=final_price,
            fill_time=dt.datetime.now() # Use actual time
          )
          logger.log("Open spread trade filled and recorded.", 
                     level=config.LOG_INFO, 
                     source=config.LOG_SOURCE_ORCHESTRATOR)

        else:
          # 3B. TIMEOUT: Cancel and Abort
          logger.log("Open spread entry timed out. Canceling order...", 
                     level=config.LOG_WARNING, 
                     source=config.LOG_SOURCE_ORCHESTRATOR)
          if server_api.cancel_order(order_id):
            logger.log("Order canceled. System remains IDLE.",                      
                       level=config.LOG_INFO,                      
                       source=config.LOG_SOURCE_ORCHESTRATOR)
          else:
            logger.log(f"CRITICAL: Failed to cancel stuck Entry Order {order_id}!", 
                       level=config.LOG_CRITICAL, 
                       source=config.LOG_SOURCE_ORCHESTRATOR)
    else:
      logger.log(f"Entry Logic Rejected: {reason}", 
                 level=config.LOG_DEBUG, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
#---------------------------------------------------#      
  elif decision_state == config.STATE_HEDGE_ADJUSTMENT_NEEDED:
    logger.log("Hedge Adjustment Required. Rolling position...", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

    old_hedge = cycle.hedge_trade_link

    # 1. Close Old Hedge
    print(f"LOG: Closing old hedge {old_hedge.id}...")
    close_res = server_api.close_position(old_hedge)
    close_order_id = close_res.get('id')
    if close_order_id:
      is_closed, close_px = server_api.wait_for_order_fill(close_order_id, timeout_seconds=10)
      if is_closed:
        realized_debit = close_px if close_px > 0 else float(close_res['price'])
        server_db.close_trade(
          trade_row=old_hedge._row,
          fill_price=realized_debit,
          fill_time=dt.datetime.now(), # Use actual time, or close_res['time'] if available/parsed
          order_id=close_order_id
        )
        logger.log("Old Hedge Closed successfully.", 
                   level=config.LOG_INFO, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
      else:
        logger.log("Hedge Close timed out. Aborting Roll.", 
                   level=config.LOG_CRITICAL, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
        # We abort here because if we can't close the old one, we shouldn't buy a new one 
        return
    else:
      logger.log("Hedge Close timed out. Aborting hedge roll.", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return
    
    # 2. Buy New Hedge
    # 1. Get Valid Expirations
    valid_dates = server_api.get_expirations()
    target_dte = cycle.rules['hedge_target_dte'] # e.g. 90
    
    best_date = server_libs.find_closest_expiration(valid_dates, target_dte)
    if not best_date: return
    
    chain = server_api.get_option_chain(date=best_date)
    if not chain: return

    # Select Strike
    leg_to_buy = server_libs.select_hedge_strike(
      chain, 
      target_delta=cycle.rules['hedge_target_delta']
    )
    if leg_to_buy:
      _execute_hedge_entry(cycle, leg_to_buy)
    else:
      logger.log("Closed old hedge but could not find new one!", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
#---------------------------------------------------#      
  elif decision_state == config.STATE_IDLE:
     logger.log("No action required.", 
                level=config.LOG_DEBUG, 
                source=config.LOG_SOURCE_ORCHESTRATOR)

  else:
    logger.log(f"Unhandled State: {decision_state}", 
               level=config.LOG_WARNING, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

def _execute_hedge_entry(cycle, leg_to_buy) -> bool:
  """
    Helper to execute, verify, and record a Hedge Entry.
    Returns True if successful, False if failed/canceled.
    """
  logger.log(f"Buying new hedge: {leg_to_buy['symbol']}", 
             level=config.LOG_INFO, 
             source=config.LOG_SOURCE_ORCHESTRATOR)

  # 1. Execute
  buy_res = server_api.buy_option(leg_to_buy)
  order_id = buy_res.get('id')
  if not order_id:
    logger.log("Hedge Order rejected by API.", 
               level=config.LOG_CRITICAL, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return False

    # 2. Verify Fill
  is_filled, fill_px = server_api.wait_for_order_fill(order_id, timeout_seconds=10)

  if is_filled:
    final_price = fill_px if fill_px > 0 else float(buy_res['price'])
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
      fill_price=final_price,
      fill_time=dt.datetime.now()
    )

    # 4. Link
    cycle._row['hedge_trade'] = new_trade_obj._row
    logger.log("Hedge executed, verified, and linked.", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return True

  else:
    # 5. Fail/Cancel
    logger.log("Hedge Market Order timed out/stuck. Canceling...", 
               level=config.LOG_CRITICAL, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    server_api.cancel_order(order_id)
    logger.log("Hedge Order Canceled.", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return False