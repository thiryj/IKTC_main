import anvil.secrets
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
  #print(f"Found {len(open_trades_list)} open trades.")
  return open_trades_list
  
@anvil.server.callable
def get_closed_trades():
  """Fetches all trades with a status of 'Closed'."""
  return tables.app_tables.trades.search(Status='Closed')

@anvil.server.callable
def find_new_diagonal_trade():
  """
  This function will contain the logic to connect to Tradier,
  find a suitable short put diagonal, and return its parameters.
  """
  print("Server function 'find_new_diagonal_trade' was called.")

  # --- PASTE YOUR TRADIER SCRIPT LOGIC HERE ---
  # For now, we will just return a hardcoded example trade.

  example_trade = {
    'underlying': 'SPY',
    'strategy': 'Short Put Diagonal',
    'short_leg': {'strike': 450, 'expiry': '2025-11-21', 'price': 5.50},
    'long_leg': {'strike': 440, 'expiry': '2025-12-19', 'price': 4.00}
  }

  return example_trade