import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
# This is a module.
# You can define variables and functions here, and use them from any form. For example, in a top-level form:
#
#    from .. import Module1
#
#    Module1.say_hello()
#
ENV_SANDBOX = 'SANDBOX'
ENV_PRODUCTION = 'PROD'
TRADE_ACTION_OPEN = 'Open'
TRADE_ACTION_ROLL = 'Roll'
TRADE_ACTION_CLOSE = 'Close'
NEW_TRADE_ACTIONS = {TRADE_ACTION_OPEN, TRADE_ACTION_ROLL}
CLOSE_TRADE_ACTIONS = {TRADE_ACTION_CLOSE}
POSITION_ACTIONS = [*NEW_TRADE_ACTIONS, *CLOSE_TRADE_ACTIONS]
POSITION_TYPES = ["Diagonal", "CSP", "Covered Call", "Stock", "Misc"]




# Globals

# data structs:
"""
trade row in open positions
{
      'trade_row': trade,
      'Underlying': trade['Underlying'],
      'Strategy': trade['Strategy'],
      'OpenDate': trade['OpenDate'],
      'extrinsic_value': None, # Placeholder
      'is_at_risk': False       # Placeholder
    }

self.trade_dto: Dict
{meta, leg1, leg2 ,(leg3), (leg4)}

roll_dto_list list of Dicts
[
{'action': 'Buy to Close', 'type': 'PUT', 'strike': 245, 'expiration': datetime.date(2025, 11, 13), 'quantity': 1}, 
{'action': 'Sell to Close', 'type': 'PUT', 'strike': 246, 'expiration': datetime.date(2025, 11, 14), 'quantity': 1}, 
{'action': 'Sell to Open', 'type': 'PUT', 'strike': 245, 'expiration': datetime.date(2025, 11, 14), 'quantity': 1}, 
{'action': 'Buy to Open', 'type': 'PUT', 'strike': 244, 'expiration': datetime.date(2025, 11, 17), 'quantity': 1}
]
"""
