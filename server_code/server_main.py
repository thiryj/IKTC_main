import anvil.email
import anvil.server
from anvil.tables import app_tables

import datetime as dt
import pytz
from typing import Optional, Tuple, Dict, List

from shared import config
from shared.classes import Cycle, Trade, Leg
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
  is_dry_run = settings_row['dry_run']
  system_settings = dict(settings_row) if settings_row else {} # <--- Force conversion
  current_env_account = config.ACTIVE_ENV # e.g., 'PROD' or 'SANDBOX'
  env_status = server_api.get_environment_status()
  today = env_status['today']
  print(f'today is: {today}')
  
  # 1. GLOBAL PRECONDITIONS
  # Check environment status (Market Open/Closed) and kill switch false BEFORE touching DB
  # This check is now purely for "Is the Market Open?" / "Is Bot Enabled globally?"
  has_trade = False
  if not server_libs.can_run_automation(env_status, system_settings,EOD_overide=has_trade):
    logger.log(f"Automation skipped. Market: {env_status.get('status_message')}", 
               level=config.LOG_DEBUG, 
               source=config.LOG_SOURCE_ORCHESTRATOR)
    return
    
  # 2. LOAD CONTEXT (or auto seed)
  print('before get_active_cycle')
  cycle = server_db.get_active_cycle(current_env_account)
  if cycle:
    has_trade = any(t for t in cycle.trades if t.status == config.STATUS_OPEN)
  else:
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
  print('before market data snapshot')
  market_data = server_api.get_market_data_snapshot(cycle)
  print(f'market: {market_data}')
    
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
      
  # 1. Get the state from our new library function
  state = server_libs.determine_scalpel_state(cycle, env_status)  
  process_state_decision(cycle, state, env_status, is_dry_run)

def process_state_decision(cycle: Cycle, 
                           decision_state: str, 
                           env_status, 
                           market_data: dict=None, 
                           is_dry_run:bool=False) -> None:
  # 2. Execute
  if decision_state == config.STATE_WAITING:
    return

  if decision_state == config.STATE_ENTRY_WINDOW:
    logger.log("Entering Scalpel Entry Window. Checking Filters...", level=config.LOG_INFO)

    # A. Fetch Market Environment (VIX/VWAP)
    mkt = server_api.get_scalpel_environment()
    if mkt['vix'] < cycle.rules.get('vix_min', 13.0):
      return # Too quiet

    # C. Filter: Directional Selection
    chain = server_api.get_option_chain(date=env_status['today'])
    candidate = server_libs.calculate_scalpel_strikes(
      chain, cycle.rules, mkt['price'], mkt['is_bullish']
    )
    vwap_pct = mkt.get('vwap_pct', 0.0)
    bias = 'CALL' if vwap_pct >= 0 else 'PUT'
    if candidate:
      _execute_scalpel_entry(cycle, candidate, is_dry_run, entry_bias=bias, vwap_pct=vwap_pct)

  elif decision_state == config.STATE_ACTIVE_HUNT:
    # Logic: We have an open trade, check if our $3.50 limit hit
    active_trades = [t for t in cycle.trades if t.status == config.STATUS_OPEN]
    if not active_trades: 
      return
    trade = active_trades[0]

    # Verify order status at broker
    status, fill_px = server_api.wait_for_order_fill(trade.order_id_external, timeout_seconds=1)

    if status == 'filled':
      server_db.close_trade(trade._row, fill_px, dt.datetime.now(dt.timezone.utc), trade.order_id_external)
      logger.log(f"SCALPEL HARVESTED: ${fill_px}", level=config.LOG_CRITICAL)

  elif decision_state == config.STATE_EOD_CLEANUP:
    active_trades = [t for t in cycle.trades if t.status == config.STATUS_OPEN]
    if not active_trades: 
      return
    trade = active_trades[0]

    logger.log("Market Closed. Executing EOD Settlement...", level=config.LOG_INFO)

    # 2. Calculate Terminal Value
    # Fetch final price of SPX
    mkt = server_api.get_scalpel_environment()
    final_px = mkt['price']

    # We need the strike to calculate payout
    # For a Bullish Call Spread: max(0, min(Width, SPX - Long_Strike))
    legs = app_tables.legs.search(trade=trade._row)
    long_leg = next(leg for leg in legs if leg['side'] == config.LEG_SIDE_LONG)
    short_leg = next(leg for leg in legs if leg['side'] == config.LEG_SIDE_SHORT)

    width = abs(short_leg['strike'] - long_leg['strike'])
    payout = 0.0

    if long_leg['option_type'] == config.TRADIER_OPTION_TYPE_CALL:
      payout = max(0, min(width, final_px - long_leg['strike']))
    else:
      payout = max(0, min(width, long_leg['strike'] - final_px))

    # 3. Settle in DB
    server_db.close_trade(
      trade_row=trade._row,
      fill_price=payout,
      fill_time=dt.datetime.now(dt.timezone.utc),
      order_id="CASH_SETTLEMENT"
    )

    logger.log(f"EOD Settlement Complete. Final Payout: ${payout:.2f}", level=config.LOG_INFO)

def process_scalpel_entry_logic(cycle: Cycle, 
                                market_env: dict, 
                                env_status: dict, 
                                is_dry_run:bool=False) -> None:
  """Standalone logic to handle the entry phase. Reachable by bot and tests."""

  # 1. VIX Check
  if market_env['vix'] < cycle.rules.get('vix_min', 13.0):
    logger.log(f"VIX too low ({market_env['vix']}). Skipping.", level=config.LOG_INFO)
    return

    # 2. Get Option Chain
  chain = server_api.get_option_chain(date=env_status['today'])

  # 3. Select Strikes
  candidate = server_libs.calculate_scalpel_strikes(
    chain, cycle.rules, market_env['price'], market_env['is_bullish']
  )
  vwap_pct = market_env.get('vwap_pct', 0.0)
  bias = 'CALL' if vwap_pct >= 0 else 'PUT'
  if not candidate:
    print("DEBUG: No spread candidate found.")
    return
  print(f"DEBUG: Candidate found! Buying {candidate['long_leg']['symbol']}")
  # This will call our DRY_RUN interceptor automatically
  _execute_scalpel_entry(cycle, candidate, is_dry_run, entry_bias=bias, vwap_pct=vwap_pct)

def _execute_scalpel_entry(cycle: Cycle, candidate: dict, is_dry_run:bool=False, entry_bias:str = None, vwap_pct:float=None) -> bool:
  """
    Quarter Kelly Sizing -> Buy Spread -> Record DB -> Place $3.50 Limit Sell.
    """
  # 1. SIZING (Quarter Kelly)
  settings = app_tables.settings.get()
  account_equity = float(settings['total_account_equity'] or 50000)

  qty = server_libs.get_scalpel_quantity(account_equity, candidate['debit'])
  candidate['quantity'] = qty

  logger.log(f"SCALPEL START: Sizing {qty} contracts for ${candidate['debit']:.2f} debit.", 
             level=config.LOG_INFO)

  # 2. BUY ENTRY
  order_res = server_api.open_spread_position(candidate, is_debit=True, is_dry_run=is_dry_run)

  # Reuse our 'Entry and Sync' logic (Mechanical Verification)
  # Note: Pass the debit as the fallback price
  new_trade = _execute_entry_and_sync(cycle, 
                                      order_res, 
                                      candidate, 
                                      config.ROLE_INCOME, 
                                      "Scalpel Entry", 
                                      fill_px_fallback=candidate['debit'],
                                      vwap_pct=vwap_pct,
                                      entry_bias=entry_bias
                                    )

  if new_trade:
    # 3. MONETIZE THE TOUCH (Immediate Limit Sell)
    leg_rows = app_tables.legs.search(trade=new_trade._row)

    # Attach them to the object so server_api.close_position can find them
    new_trade.legs = [Leg(l_row) for l_row in leg_rows]

    harvest_target = float(cycle.rules.get('harvest_target', 3.50))

    logger.log(f"ENTRY CONFIRMED. Placing monetization limit sell at ${harvest_target:.2f}", 
               level=config.LOG_INFO)

    # Place the $3.50 Limit Order immediately
    # We'll use a modified close_position that accepts a limit price
    exit_res = server_api.close_position(new_trade, order_type='limit', limit_price=harvest_target, is_dry_run=is_dry_run)

    if exit_res.get('id'):
      # Update the trade row with the active Order ID so we can track it
      new_trade._row['order_id_external'] = exit_res['id']
      new_trade._row['target_harvest_price'] = harvest_target
      return True
    else:
      logger.log("CRITICAL: Failed to place monetization sell order!", level=config.LOG_CRITICAL)

  return False

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
                            fill_px_fallback: float=0.0,
                            vwap_pct: float=0.0,
                           entry_bias: str = None) -> bool:
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
      fill_time=dt.datetime.now(dt.timezone.utc),
      vwap_pct=vwap_pct,
      entry_bias=entry_bias
    )
      
    logger.log(f"SUCCESS: {action_desc} filled at ${final_px}", level=config.LOG_INFO)
    return new_trade

    # 3. SAFETY: If entry didn't fill, we MUST cancel it on broker 
    # so we don't accidentally fill later and desync.
  logger.log(f"TIMEOUT: {action_desc} failed. Canceling order...", level=config.LOG_WARNING)
  server_api.cancel_order(order_id)
  return None

