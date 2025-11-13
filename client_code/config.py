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

# Environment
ENV_SANDBOX = 'SANDBOX'
ENV_PRODUCTION = 'PROD'

# Option types
OPTION_TYPE_PUT = 'PUT'
OPTION_TYPE_CALL = 'CALL'

# Trade Actions
TRADE_ACTION_OPEN = 'Open'
TRADE_ACTION_ROLL = 'Roll'
TRADE_ACTION_CLOSE = 'Close'
NEW_TRADE_ACTIONS = {TRADE_ACTION_OPEN}
CLOSE_TRADE_ACTIONS = {TRADE_ACTION_ROLL, TRADE_ACTION_CLOSE}
POSITION_ACTIONS = [*NEW_TRADE_ACTIONS, *CLOSE_TRADE_ACTIONS]

# Position Types
POSITION_TYPE_DIAGONAL = 'Diagonal'
POSITION_TYPE_CSP = 'CSP'
POSITION_TYPE_COVERED_CALL = 'Covered Call'
POSITION_TYPE_STOCK = 'Stock'
POSITION_TYPE_MISC = 'Misc'
POSITION_TYPES = [POSITION_TYPE_DIAGONAL, POSITION_TYPE_CSP, POSITION_TYPE_COVERED_CALL, POSITION_TYPE_STOCK, POSITION_TYPE_MISC]

MANUAL_ENTRY_STATE_OPEN = 'OPEN'
MANUAL_ENTRY_STATE_EDIT = 'EDIT'

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
