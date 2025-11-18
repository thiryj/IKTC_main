import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server

ALLOCATION = 10000
MAX_DTE = 90
LONG_STRIKE_DELTA_MAX = 25  # how much lower than short strike to search
ASSIGNMENT_RISK_THRESHOLD = 0.1

#ENUMS and sets
SHORT_OPEN_ACTION = 'Sell to Open'
SHORT_CLOSE_ACTION = 'Buy to Close'
LONG_OPEN_ACTION = 'Buy to Open'
LONG_CLOSE_ACTION = 'Sell to Close'
OPEN_ACTIONS = {SHORT_OPEN_ACTION, LONG_OPEN_ACTION}
CLOSE_ACTIONS = {SHORT_CLOSE_ACTION, LONG_CLOSE_ACTION}
OPTION_TYPE_PUT = 'PUT'
OPTION_TYPE_CALL = 'CALL'
ENV_SANDBOX = 'SANDBOX'
ENB_PRODUCTION = 'PROD'
TRADE_ROW_STATUS_OPEN = 'Open'
TRADE_ROW_STATUS_CLOSED = 'Closed'

# server globals - BAD IDEA - do not use server side globals.  server is stateless



