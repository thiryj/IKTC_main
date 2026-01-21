import anvil.email
import anvil.server
from anvil.tables import app_tables

import datetime as dt
import pytz
from typing import Optional, Tuple, Dict, List

from shared import config
from shared.classes import Cycle, Trade
from . import server_libs  # The Brains (Clean Stubs)
from . import server_api  # The Hands (Dirty Stubs)
from . import server_db, server_logging as logger

@anvil.server.callable
@anvil.server.background_task
@anvil.tables.in_transaction
def run_automation_routine():    
  '''
  logger.log("Starting Automation Run ...",
    level=config.LOG_INFO,
    source=config.LOG_SOURCE_ORCHESTRATOR)
  '''
  settings_row = app_tables.settings.get() 
  if settings_row:
    settings_row['last_bot_heartbeat'] = dt.datetime.now()
    system_settings = dict(settings_row)
  else:
    system_settings = {}
    
  current_env_account = config.ACTIVE_ENV # e.g., 'PROD' or 'SANDBOX'
  # 1. GLOBAL PRECONDITIONS
  # Check environment status (Market Open/Closed) and kill switch false BEFORE touching DB
  env_status = server_api.get_environment_status()

  # This check is now purely for "Is the Market Open?" / "Is Bot Enabled globally?"
  if not server_libs.can_run_automation(env_status, system_settings):
    logger.log(f"Automation skipped. Market: {env_status.get('status_message')}", 
               level=config.LOG_DEBUG, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return
    
  # 2. LOAD CONTEXT (or auto seed)
  cycle = server_db.get_active_cycle(current_env_account)
  if not cycle:
    if server_db.check_cycle_closed_today(config.ACTIVE_ENV):
      logger.log("System Idle - Cycle closed today (Panic/Manual). Waiting for tomorrow.", 
                 level=config.LOG_DEBUG, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      return
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
  print(f'market: {market_data}')
  decision_state = server_libs.determine_cycle_state(cycle, market_data, env_status)
  if decision_state != config.STATE_IDLE:
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
    for trade in income_trades:
      order_res = server_api.close_position(trade, order_type='market')
      success = _execute_settlement_and_sync(trade, order_res, "Panic Spread Exit")
      if not success:
        liabilities_cleared = False
        logger.log(f"ALERT: Failed to confirm spread {trade.id} closed. Holding Hedge.", 
                   level=config.LOG_CRITICAL)
      
      # 3. Phase 2: Close Hedges (Only if spreads are confirmed gone)  
      if liabilities_cleared:
        for trade in hedge_trades:
          order_res = server_api.close_position(trade, order_type='market')

          # Use helper
          h_success = _execute_settlement_and_sync(trade, order_res, "Panic Hedge Exit")

          if h_success:
            # 4. Campaign Logic: Close the cycle
            logger.log("Panic Harvest Complete. Closing Cycle.", level=config.LOG_INFO)
            # This worker sets end_date, total_pnl, and status=CLOSED
            server_db.close_active_cycle(cycle.id) 
      else:
        logger.log("PANIC ABORTED: Liabilities still present in DB. System holding Hedge for protection.", level=config.LOG_CRITICAL)
        
    # 3. Phase 2: Close Assets (Hedge)
    # ONLY proceed if we successfully submitted close orders for all liabilities
    if liabilities_cleared:
      logger.log("Liabilities cleared. Closing Hedges...", 
                 level=config.LOG_INFO, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)
      for h_trade in hedge_trades:
        logger.log(f"Closing Hedge Asset {trade.id}...", 
                   level=config.LOG_INFO, 
                   source=config.LOG_SOURCE_ORCHESTRATOR)
        
        h_order = server_api.close_position(h_trade, order_type='market')
        h_success = _execute_settlement_and_sync(h_trade, h_order, "Panic Hedge Exit")
        if h_success:
          # --- STAGE 4: TERMINAL ACTION ---
          logger.log("Hedge settled. Finalizing Cycle.", level=config.LOG_INFO)
          # This function sets end_date, calculates total_pnl, and sets status=CLOSED
          server_db.close_active_cycle(cycle.id)
               
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
    settled = _execute_settlement_and_sync(spread_trade, close_res, "Roll Exit")

    if settled:
      # --- STEP 2: RE-ENTRY LOGIC ---
      # A. Check Safety (Don't re-enter if market is crashing)
      is_safe, safety_msg = server_libs.check_roll_safety(market_data, cycle.rules)
      if not is_safe:
        logger.log(f"Roll Re-Entry Aborted: {safety_msg}. Staying Flat.", 
                  level=config.LOG_WARNING, 
                  source=config.LOG_SOURCE_ORCHESTRATOR)
        return

      roll_result, target_date = _find_best_roll_candidate(cycle, spread_trade, market_data)
      if roll_result:
        # --- STEP 3: OPEN NEW (Asset) ---
        trade_data = {
          'quantity': spread_trade.quantity,
          'short_strike': roll_result['short_leg']['strike'],
          'long_strike': roll_result['long_leg']['strike'],
          'short_leg_data': roll_result['short_leg'],
          'long_leg_data': roll_result['long_leg'],
          'net_credit': roll_result['new_credit']
        }

        order_res = server_api.open_spread_position(trade_data)
        
        entered = _execute_entry_and_sync(cycle, order_res, trade_data, config.ROLE_INCOME, "Roll Entry")
        if entered:
          _reset_cycle_hedge_reference(cycle, market_data)
      else:
        logger.log("Roll Aborted: No valid strikes found to cover costs. Staying Flat.", 
                   level=config.LOG_WARNING)

#---------------------------------------------------#
  elif decision_state == config.STATE_NAKED_HEDGE_HARVEST:
    hedge_trade = cycle.hedge_trade_link
    order_res = server_api.close_position(hedge_trade, order_type='market')

    # Post processing
    success = _execute_settlement_and_sync(hedge_trade, order_res, "Naked Hedge Harvest")

    # Close Cycle
    if success:
      logger.log("Hedge Harvested. Closing the current Cycle.", level=config.LOG_INFO)
      server_db.close_active_cycle(cycle.id) # Assuming a helper that sets end_date/status
      
      logger.log(f"Cycle Closed via Windfall Harvest. Final PnL: ${total_dollars:+.2f}", 
                 level=config.LOG_CRITICAL, 
                 source=config.LOG_SOURCE_ORCHESTRATOR)

#---------------------------------------------------#
  elif decision_state == config.STATE_HARVEST_TARGET_HIT:
    trade = server_libs.get_winning_spread(cycle, market_data)
    if trade:
      order_res = server_api.close_position(trade)
      _execute_settlement_and_sync(trade, order_res, "Profit Harvest")
      
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
      order_res = server_api.open_spread_position(trade_data)
      _execute_entry_and_sync(cycle, order_res, trade_data, config.ROLE_INCOME, "Standard Spread Entry")
      
#---------------------------------------------------#      
  elif decision_state == config.STATE_HEDGE_ADJUSTMENT_NEEDED:
    logger.log("Hedge Adjustment Required. Rolling position...", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)

    old_hedge = cycle.hedge_trade_link

    # 1. Close Old Hedge
    close_res = server_api.close_position(old_hedge)
    settled = _execute_settlement_and_sync(old_hedge, close_res, "Hedge Roll Exit")
    if settled:
      valid_dates = server_api.get_expirations()
      target_dte = cycle.rules.get('hedge_target_dte', 90)
      
      best_date = server_libs.find_closest_expiration(valid_dates, target_dte)
      if not best_date:
        logger.log("Hedge Roll Aborted: No valid expirations found.", level=config.LOG_CRITICAL)
        return

      chain = server_api.get_option_chain(date=best_date)
      if not chain: return

      leg_to_buy = server_libs.select_hedge_strike(
        chain, 
        target_delta=cycle.rules.get('hedge_target_delta', 0.25)
      )
      if leg_to_buy:
        trade_data = {
          'quantity': 1,
          'short_strike': 0,
          'long_strike': leg_to_buy['strike'],
          'short_leg_data': {}, 
          'long_leg_data': leg_to_buy
        }

        order_res = server_api.buy_option(leg_to_buy)
        entered = _execute_entry_and_sync(
          cycle, 
          order_res, 
          trade_data, 
          config.ROLE_HEDGE, 
          "Hedge Roll Entry")
        if entered:
          # Post-Logic: Reset the daily hedge reference to the new purchase price
          _reset_cycle_hedge_reference(cycle, market_data)
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
  status, fill_px = server_api.wait_for_order_fill(order_id, timeout_seconds=10)

  if status == 'filled':
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
    logger.log(f"Hedge Market Order timed out/stuck with status {status}. Canceling...", 
               level=config.LOG_CRITICAL, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    server_api.cancel_order(order_id)
    logger.log("Hedge Order Canceled.", 
               level=config.LOG_INFO, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return False

# In server_main.py (Private helper)
def _execute_settlement_and_sync(trade_obj: Trade, order_res: dict, action_desc: str, close_cycle: bool = False) -> bool:
  """
  Unified handler for all position exits. 
  Synchronizes Broker fill with DB Settlement. (wait and record)
  """
  order_id = order_res.get('id')
  if not order_id:
    logger.log(f"FAILED: {action_desc} rejected by API.", level=config.LOG_CRITICAL)
    return False

  # 1. Wait (No db lock)
  status, fill_px = server_api.wait_for_order_fill(order_id, config.ORDER_TIMEOUT_SECONDS)
  if status == 'filled':
    # 2. Record (Inside Transaction - via CRUD function)
    try:
      final_px = fill_px if fill_px > 0 else float(order_res['price'])
      server_db.crud_settle_trade_manual(
        trade_id=trade_obj.id,
        data={'exit_price': final_px, 
              'exit_time': dt.datetime.now(dt.timezone.utc), 
              'notes': f"[AUTO] {action_desc}"},
        close_cycle=close_cycle
      )
      logger.log(f"SUCCESS: {action_desc} recorded at ${final_px}", level=config.LOG_INFO)
      return True
    except Exception as e:
      logger.log(f"DB SYNC ERROR: {action_desc} filled at broker but failed to record in DB: {e}", 
                 level=config.LOG_CRITICAL)
      return False
  return False

# In server_main.py (Private helper)
def _execute_entry_and_sync(cycle: Cycle, order_res: dict, trade_data: dict, role: str, action_desc: str) -> bool:
  """
    Unified handler for all position entries.
    Includes Safety: Cancels order on broker if timeout occurs.
    """
  order_id = order_res.get('id')
  if not order_id: return False

    # 1. Wait
  status, fill_px = server_api.wait_for_order_fill(order_id, config.ORDER_TIMEOUT_SECONDS)

  if status == 'filled':
    # 2. Record (Inside Transaction)
    final_px = fill_px if fill_px > 0 else float(order_res['price'])
    new_trade = server_db.record_new_trade(
      cycle_row=cycle._row,
      role=role,
      trade_dict=trade_data,
      order_id=order_id,
      fill_price=final_px,
      fill_time=dt.datetime.now(dt.timezone.utc)
    )

    # Link hedge specifically
    if role == config.ROLE_HEDGE:
      cycle._row['hedge_trade'] = new_trade._row

    logger.log(f"SUCCESS: {action_desc} filled at ${final_px}", level=config.LOG_INFO)
    return True

    # 3. SAFETY: If entry didn't fill, we MUST cancel it on broker 
    # so we don't accidentally fill later and desync.
  logger.log(f"TIMEOUT: {action_desc} failed. Canceling order...", level=config.LOG_WARNING)
  server_api.cancel_order(order_id)
  return False

def _find_best_roll_candidate(cycle: Cycle, old_trade: Trade, market_data: dict) -> Tuple[Optional[dict], Optional[dt.date]]:
  """
    Hunts across expirations to find a roll that satisfies the credit requirement.
    Returns (roll_result_dict, target_date)
    """
  # 1. Identify the 'Line in the Sand' (Current Short Strike)
  # We must roll DOWN, so the new short must be lower than this.
  legs = getattr(old_trade, 'legs', [])
  current_short = next((l for l in legs if l.side == config.LEG_SIDE_SHORT), None)
  if not current_short:
    logger.log("ROLL ERROR: Could not identify short leg for strike comparison.", level=config.LOG_CRITICAL)
    return None, None

    # 2. Identify the 'Debt' we need to cover
    # We use the mark from market_data (the price we just paid to exit)
  realized_debit = market_data.get('spread_marks', {}).get(old_trade.id, 0.0)

  # 3. Get valid dates and scan (T+1 through T+3)
  valid_dates = server_api.get_expirations()
  retry_offsets = [1, 2, 3] 

  for days in retry_offsets:
    candidate_date = server_libs.find_closest_expiration(valid_dates, target_dte=days)
    if not candidate_date: continue

    chain = server_api.get_option_chain(date=candidate_date)
    if not chain: continue

      # Call the math worker in libs to find the best strikes on this date
    result = server_libs.calculate_roll_legs(
      chain=chain,
      current_short_strike=current_short.strike,
      width=cycle.rules['spread_width'],
      cost_to_close=realized_debit
    )

    if result:
      logger.log(f"Roll Found: {candidate_date} (T+{days}) at ${result['new_credit']:.2f} credit", level=config.LOG_INFO)
      return result, candidate_date

  return None, None

def _reset_cycle_hedge_reference(cycle: Cycle, market_data: dict) -> None:
  """
  Updates the Cycle's daily_hedge_ref to the current market price.
  This 'zeroes out' the hedge PnL for the Panic Harvest calculation.
  """
  current_hedge_px = market_data.get('hedge_last', 0.0)

  if current_hedge_px > 0:
    try:
      # Update the DB row directly
      cycle._row['daily_hedge_ref'] = current_hedge_px
      logger.log(f"Hedge Reference reset to ${current_hedge_px:.2f} for Cycle {cycle.id}", 
                 level=config.LOG_INFO, source=config.LOG_SOURCE_ORCHESTRATOR)
    except Exception as e:
      logger.log(f"Failed to reset hedge reference: {e}", level=config.LOG_WARNING)