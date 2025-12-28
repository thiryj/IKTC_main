from shared import config

def can_run_automation(env, cycle):
  # STUB: Always say yes for testing
  return True

def is_db_consistent(cycle, positions):
  # STUB: Assume DB is perfect
  return True

def determine_cycle_state(cycle, market_data):
  # STUB: Return different states here to test the flow in server_main
  # return config.STATE_HEDGE_MISSING
  # return config.STATE_PANIC_HARVEST
  return config.STATE_IDLE

def alert_human(message, level=config.ALERT_INFO):
  print(f"ALERT [{level}]: {message}")

# ... Add other stubs (select_hedge_strike, calculate_roll_legs) as we hit them