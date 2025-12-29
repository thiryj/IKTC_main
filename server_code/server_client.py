import anvil.server
from anvil.tables import app_tables

from typing import Optional, Dict, Any

from . import server_db
from . import server_main
from . import server_api
from shared import config
from shared.classes import Cycle

# ---All the human commands that come from UI or console
@anvil.server.callable
def get_campaign_dashboard()->Optional[Cycle]:
  """Returns the 'Clean' Cycle object for the UI/Console to render."""
  return server_db.get_active_cycle()
  
@anvil.server.callable
def start_new_cycle(account_name:str, underlying_symbol:str, rule_set_id:str)->Cycle:
  """Initializes a new campaign."""
  rule_set_row = app_tables.rulesets.get_by_id(rule_set_id)
  new_cycle = server_db.create_new_cycle(account_name, underlying_symbol, rule_set_row)
  return new_cycle

@anvil.server.callable
def run_auto()->str:
  """Forces the automation logic to run NOW. Useful for testing or impatience."""
  print("LOG: Manual Trigger received.")
  server_main.run_automation_routine()
  return "Automation Run Complete"

@anvil.server.callable
def close_campaign_manual(cycle_id:str)-> bool:
  """The 'Eject' button."""
  cycle = server_db.get_cycle_by_id(cycle_id)
  if not cycle:
    return False

  print(f"LOG: Manually closing Cycle {cycle.id}...")
  server_api.close_all_positions(cycle)
  cycle.status = config.STATUS_CLOSED
  server_db.save_cycle(cycle)
  # Optional: server_api.close_all_positions(cycle)
  return True
