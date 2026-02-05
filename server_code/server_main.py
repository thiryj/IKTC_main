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
def run_automation_routine():    
  '''
  logger.log("Starting Automation Run ...",
    level=config.LOG_INFO,
    source=config.LOG_SOURCE_ORCHESTRATOR)
  '''
  #print('run_automation_routine: start')
  if _set_processing_lock(True):
    return
  #print('run_automation_routine: after _set_processing_lock, executing loop')
  try:
    _execute_automation_loop()
    print('run_automation_routine: after executing loop')
    
  except Exception as e:
    logger.log(f"CRITICAL: Automation loop crashed: {e}", level=config.LOG_CRITICAL)

  finally:
    _set_processing_lock(False)

@anvil.tables.in_transaction
def _set_processing_lock(value: bool) -> bool:
  """
    Helper function to flip the lock bit. 
    Returns the PREVIOUS state of the lock.
    """
  settings = app_tables.settings.get()
  current_state = settings['processing_lock']

  # If we are trying to set lock to True, but it's already True, 
  # we should let the caller know it was already busy.
  if value is True and current_state is True:
    return True # Already busy

  settings['processing_lock'] = value
  if value is True:
    settings['last_bot_heartbeat'] = dt.datetime.now(dt.timezone.utc)

  return current_state

def _execute_automation_loop():
  settings_row = app_tables.settings.get()  
  system_settings = dict(settings_row) if settings_row else {} # <--- Force conversion
  current_env_account = config.ACTIVE_ENV # e.g., 'PROD' or 'SANDBOX'
  # 1. GLOBAL PRECONDITIONS
  # Check environment status (Market Open/Closed) and kill switch false BEFORE touching DB
  env_status = server_api.get_environment_status()
  today = env_status['today']
  print(f'today is: {today}')
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
  
  market_data = server_api.get_market_data_snapshot(cycle)
  print(f'market: {market_data}')
  # We only do this reset once per day (at the first heartbeat after open)
  current_h_px = market_data.get('hedge_last', 0.0)
  
  if system_settings['last_reference_reset'] and system_settings['last_reference_reset'] != today and current_h_px > 0:
    cycle._row['daily_hedge_ref'] = current_h_px
    settings_row['last_reference_reset'] = today
    logger.log(f"Daily Sync: Hedge Reference reset to Morning Mark (${current_h_px:.2f})", level=config.LOG_INFO)
    
  # 3. SYNC REALITY (Dirty)
  # Ensure DB matches Tradier before making decisions
  if config.ENFORCE_ZOMBIE_CHECKS:
    positions = server_api.get_current_positions()

    # check for positions open in db that are not in broker
    zombies = server_libs.get_zombie_trades(cycle, positions)
    if zombies:
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
  if config.ENFORCE_CONSISTENCY_CHECKS:
    positions = server_api.get_current_positions()
    if not server_libs.is_db_consistent(cycle, positions):
      #TODO:  this is currently a stub that always returns true
      # Stop everything if the map doesn't match the territory
      logger.log("DB/Broker Mismatch Detected (Non-Critical). Proceeding...", 
                level=config.LOG_CRITICAL, 
                source=config.LOG_SOURCE_ORCHESTRATOR)
      return

  # 4. DETERMINE STATE
  # The brain analyzes the cycle + market data and returns ONE state constant
  decision_state = server_libs.determine_cycle_state(cycle, market_data, env_status, system_settings)
  if decision_state != config.STATE_IDLE:
    logger.log(f"Decision State -> {decision_state}", 
              level=config.LOG_INFO, 
              source=config.LOG_SOURCE_ORCHESTRATOR, 
              context={'cycle_id': cycle.id})

    process_state_decision(cycle, decision_state, market_data, env_status)

def process_state_decision(cycle: Cycle, decision_state: str, market_data: dict, env_status: dict) -> None:
  # 5. EXECUTE
  if True:
    pass
  elif decision_state == config.STATE_IDLE:
     logger.log("No action required.", 
                level=config.LOG_DEBUG, 
                source=config.LOG_SOURCE_ORCHESTRATOR)

  else:
    logger.log(f"Unhandled State: {decision_state}", 
               level=config.LOG_WARNING, 
               source=config.LOG_SOURCE_ORCHESTRATOR)


# In server_main.py (Private helper)
def _execute_settlement_and_sync(trade_obj: Trade, order_res: dict, action_desc: str, close_cycle: bool = False, fill_px_fallback: float=0.0) -> bool:
  """
  Unified handler for all position exits. 
  Synchronizes Broker fill with DB Settlement. (wait and record)
  """
  order_id = order_res.get('id')
  if not order_id:
    logger.log(f"FAILED: {action_desc} rejected by API.", level=config.LOG_CRITICAL)
    return False

  # 1. Wait (No db lock)
  status, fill_px = server_api.wait_for_order_fill(order_id, config.ORDER_TIMEOUT_SECONDS, fill_px_fallback)
  if status == 'filled':
    # 2. Record (Inside Transaction - via CRUD function)
    try:
      final_px = fill_px if fill_px > 0 else fill_px_fallback
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
def _execute_entry_and_sync(cycle: Cycle, 
                            order_res: dict, 
                            trade_data: dict, 
                            role: str, 
                            action_desc: str, 
                            entry_reason: str = None, 
                            fill_px_fallback: float=0.0) -> bool:
  """
    Unified handler for all position entries.
    Includes Safety: Cancels order on broker if timeout occurs.
    """
  order_id = order_res.get('id')
  if not order_id: 
    return False

    # 1. Wait
  status, fill_px = server_api.wait_for_order_fill(order_id, config.ORDER_TIMEOUT_SECONDS, fill_px_fallback)

  if status == 'filled':
    # 2. Record (Inside Transaction)
    final_px = fill_px if fill_px != 0 else float(order_res['price'])
    new_trade = server_db.record_new_trade(
      cycle_row=cycle._row,
      role=role,
      entry_reason=entry_reason,
      trade_dict=trade_data,
      order_id=order_id,
      fill_price=final_px,
      fill_time=dt.datetime.now(dt.timezone.utc)
    )

    # Link hedge specifically
    if role == config.ROLE_HEDGE:
      cycle._row['hedge_trade'] = new_trade._row
      cycle._row['daily_hedge_ref'] = final_px
      
    logger.log(f"SUCCESS: {action_desc} filled at ${final_px}", level=config.LOG_INFO)
    return True

    # 3. SAFETY: If entry didn't fill, we MUST cancel it on broker 
    # so we don't accidentally fill later and desync.
  logger.log(f"TIMEOUT: {action_desc} failed. Canceling order...", level=config.LOG_WARNING)
  server_api.cancel_order(order_id)
  return False

