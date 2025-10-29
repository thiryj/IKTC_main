import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

# This is a server module. It runs on the Anvil server,
# rather than in the user's browser.
#
# To allow anvil.server.call() to call functions here, we mark
#
@anvil.server.callable
def get_open_trades():
  """Fetches all trades with a status of 'Open'."""
  open_trades_list = list(tables.app_tables.trades.search(Status=q.full_text_match('Open')))
  print(f"Found {len(open_trades_list)} open trades.")
  return open_trades_list
  

@anvil.server.callable
def get_closed_trades():
  """Fetches all trades with a status of 'Closed'."""
  return tables.app_tables.trades.search(Status='Closed')