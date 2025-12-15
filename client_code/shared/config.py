
# server side configs to port over
ALLOCATION = 10000
#MAX_DTE = 90
LONG_STRIKE_DELTA_MAX = 25  # how much lower than short strike to search
ASSIGNMENT_RISK_THRESHOLD = 0.1

# Shared config
# Environment
ENV_SANDBOX = 'SANDBOX'
ENV_PRODUCTION = 'PROD'

# Position Types
POSITION_TYPE_VERTICAL = 'VERTICAL'
POSITION_TYPES = [POSITION_TYPE_VERTICAL]
POSITION_TYPES_ACTIVE = [POSITION_TYPE_VERTICAL]

# Defaults
DEFAULT_SYMBOL = 'SPX'
DEFAULT_QUANTITY = 1

# Spread Defaults
DEFTAULT_WIDTH = 25
DEFAULT_VERTICAL_DELTA = -0.20
DEFAULT_MULTIPLIER = 100
DEFAULT_HARVEST_TARGET = 0.50   # % of current spread premium

#Roll Defaults
MAX_DTE = 90

# Hedge Defaults
DEFAULT_HEDGE_DELTA = -.25
DEFAULT_HEDGE_RATIO = 0.20   # 1 hedge per 5 spreads
DEFAULT_HEDGE_DTE = 90

# Tradier Enums
TRADIER_INTERVAL_TICK = 'tick'
TRADIER_INTERVAL_1MIN = '1min'
TRADIER_INTERVAL_5MIN = '5min'
TRADIER_INTERVAL_15MIN = '15min'

# Flags
TRADE_ONE = True

# Option types
OPTION_TYPE_PUT = 'PUT'
OPTION_TYPE_CALL = 'CALL'

# Trade Table
TRADE_ACTION_OPEN = 'OPEN'
TRADE_ACTION_CLOSE = 'CLOSE'

# Leg actions
ACTION_SELL_TO_OPEN = 'Sell to Open'
ACTION_BUY_TO_OPEN = 'Buy to Open'
ACTION_SELL_TO_CLOSE = 'Sell to Close'
ACTION_BUY_TO_CLOSE = 'Buy to Close'
OPEN_ACTIONS = {ACTION_SELL_TO_OPEN, ACTION_BUY_TO_OPEN}
CLOSE_ACTIONS = {ACTION_BUY_TO_CLOSE, ACTION_SELL_TO_CLOSE}

MANUAL_ENTRY_STATE_OPEN = 'OPEN'
MANUAL_ENTRY_STATE_CLOSE = 'CLOSE'
MANUAL_ENTRY_STATE_ROLL = 'ROLL'
MANUAL_ENTRY_DEFAULT_POSITION_TYPE = POSITION_TYPE_VERTICAL

TRADE_TICKET_STATE_OPEN = 'OPEN'
TRADE_TICKET_STATE_CLOSE = 'CLOSE'
TRADE_TICKET_STATE_ROLL = 'ROLL'

# Campaings
CAMPAIGN_AUTO_PRE = "PRE-AUTO"   #used app to select and send open verts, but manually closed/rolled.  subject to human emotion + error
CAMPAIGN_AUTO_SEMI = "SEMI-AUTO" #app now sends harvest orders and panic close orders (rolls?)
CAMPAIGN_AUTO_FULL = "FULL-AUTO" #app now executes all rules including hedge management w/o human intervention
CAMPAIGN_ALL = [CAMPAIGN_AUTO_PRE, CAMPAIGN_AUTO_SEMI, CAMPAIGN_AUTO_FULL]

#INDEX_SYMBOLS = ['SPX', 'NDX', 'RUT', 'VIX']

# Globals
REFRESH_TIMER_INTERVAL = 3600

# data structs:
"""
trade row in open positions
trade_dto = {
      'trade_row': trade,
      'Underlying': trade['Underlying'],
      'Strategy': trade['Strategy'],
      'Quantity': None,
      'OpenDate': trade['OpenDate'],
      'extrinsic_value': None, # Placeholder
      'is_at_risk': False,       # Placeholder
      'short_strike': None,
      'long_strike': None,
      'short_expiry': None,
      'rroc': "N/A",
      'is_harvestable': False
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

"""
my class's dto returned from myposition.get_dto()
position_dto = {
      # --- Top-level calculated metrics ---
      'net_premium': self.net_premium,
      'margin': self.margin,
      'ROM': self.ROM,
      'short_put_DTE': self.short_put_DTE,
      'ROM_rate': self.ROM_rate,

      # --- Nested dictionary for the short put leg ---
      # We assume the 'Quote' object has these attributes based on your methods
      'short_put': {
        'symbol': self.short_put.symbol,
        'option_type': self.short_put.option_type.name,
        'strike': self.short_put.strike,
        'expiration_date': self.short_put.expiration_date,
        'bid': self.short_put.bid,
        'ask': self.short_put.ask,
        'last': self.short_put.last,
        'contract_size': self.short_put.contract_size
      },

      # --- Nested dictionary for the long put leg ---
      'long_put': {
        'symbol': self.long_put.symbol,
        'option_type': self.long_put.option_type.name,
        'strike': self.long_put.strike,
        'expiration_date': self.long_put.expiration_date,
        'bid': self.long_put.bid,
        'ask': self.long_put.ask,
        'last': self.long_put.last,
        'contract_size': self.long_put.contract_size
      }
    }
    return position_dto
      
"""
