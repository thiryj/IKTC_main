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
UNDERLYING_SYMBOL = "IWM"
TRADE_TYPE_OPEN = 'open'
TRADE_TYPE_ROLL = 'roll'
NEW_TRADE_TYPES = {'Open: Diagonal', 'Open: Cash Secured Put'}


# Globals

# Client side helpers