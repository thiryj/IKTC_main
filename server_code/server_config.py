import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

ALLOCATION = 10000
MAX_DTE = 90
LONG_STRIKE_DELTA_MAX = 25  # how much lower than short strike to search
ASSIGNMENT_RISK_THRESHOLD = 0.1

#FLAGS
PLACE_TRADE = True
TRADE_ONE = True
USER_CONFIRMATION = True
DAYS_TO_NOT_OPEN = (4,)  # data shows don't open bullish positions on Friday because prices are statistically higher
ROLL = False

#ENUMS and sets
OPEN_ACTIONS = {'Sell to Open', 'Buy to Open'}
CLOSE_ACTIONS = {'Sell to Close', 'Buy to Close'}
OPTION_TYPE_PUT = 'Put'
OPTION_TYPE_CALL = 'Call'
