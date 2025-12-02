# This is a module.
# You can define variables and functions here, and use them from any form. For example, in a top-level form:
#
#    from .. import Module1
#
#    Module1.say_hello()
#
# - hello me at git

# Environment
ENV_SANDBOX = 'SANDBOX'
ENV_PRODUCTION = 'PROD'

# Tradier Enums
TRADIER_INTERVAL_TICK = 'tick'
TRADIER_INTERVAL_1MIN = '1min'
TRADIER_INTERVAL_5MIN = '5min'
TRADIER_INTERVAL_15MIN = '15min'

# Default underlying symbol
DEFAULT_SYMBOL = 'RUT'
DEFAULT_QUANTITY = 1
DEFAULT_MULTIPLIER = 100
DEFAULT_RROC_HARVEST_TARGET = 0.02

# Logic defaults
DAYS_TO_NOT_OPEN = (4,)  # data shows don't open bullish positions on Friday because prices are statistically higher 
VERTICAL_SPREADS_ONLY = False

# Flags
TRADE_ONE = True
VERTICAL_SPREADS_ONLY = False

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

# Leg actions
ACTION_SELL_TO_OPEN = 'Sell to Open'
ACTION_BUY_TO_OPEN = 'Buy to Open'
ACTION_SELL_TO_CLOSE = 'Sell to Close'
ACTION_BUY_TO_CLOSE = 'Buy to Close'
OPEN_ACTIONS = {ACTION_SELL_TO_OPEN, ACTION_BUY_TO_OPEN}
CLOSE_ACTIONS = {ACTION_BUY_TO_CLOSE, ACTION_SELL_TO_CLOSE}

# Position Types
POSITION_TYPE_DIAGONAL = 'Diagonal'
POSITION_TYPE_CSP = 'CSP'
POSITION_TYPE_COVERED_CALL = 'Covered Call'
POSITION_TYPE_STOCK = 'Stock'
POSITION_TYPE_MISC = 'Misc'
POSITION_TYPES = [POSITION_TYPE_DIAGONAL, POSITION_TYPE_CSP, POSITION_TYPE_COVERED_CALL, POSITION_TYPE_STOCK, POSITION_TYPE_MISC]

MANUAL_ENTRY_STATE_OPEN = 'OPEN'
MANUAL_ENTRY_STATE_CLOSE = 'CLOSE'
MANUAL_ENTRY_STATE_ROLL = 'ROLL'
MANUAL_ENTRY_DEFAULT_POSITION_TYPE = POSITION_TYPE_DIAGONAL

TRADE_TICKET_STATE_OPEN = 'OPEN'
TRADE_TICKET_STATE_CLOSE = 'CLOSE'
TRADE_TICKET_STATE_ROLL = 'ROLL'

# Globals
REFRESH_TIMER_INTERVAL = 3600

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
meta: 'spread_action': [TRADE_ACTION_OPEN, TRADE_ACTION_CLOSE], 'net_premium': 0.41, 'margin': 59.00000000000001, 'ROM': 0.00694915254237288, 'short_put_DTE': 1, 'ROM_rate': 0.00694915254237288

roll_dto_list list of Dicts
[
{'action': 'Buy to Close', 'type': 'PUT', 'strike': 245, 'expiration': datetime.date(2025, 11, 13), 'quantity': 1}, 
{'action': 'Sell to Close', 'type': 'PUT', 'strike': 246, 'expiration': datetime.date(2025, 11, 14), 'quantity': 1}, 
{'action': 'Sell to Open', 'type': 'PUT', 'strike': 245, 'expiration': datetime.date(2025, 11, 14), 'quantity': 1}, 
{'action': 'Buy to Open', 'type': 'PUT', 'strike': 244, 'expiration': datetime.date(2025, 11, 17), 'quantity': 1}
]
"""
