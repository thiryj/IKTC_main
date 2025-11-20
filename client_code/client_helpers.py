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

from . import config

class LiveSettings:
  def __init__(self, row):
    # Use super to avoid recursion error when setting _row
    super().__setattr__("_row", row)

    # --- Dot Notation Support (for your code) ---
  def __getattr__(self, name):
    try:
      return self._row[name]
    except KeyError:
      raise AttributeError(name)

  def __setattr__(self, name, value):
    self._row[name] = value

    # --- Bracket Support (for Anvil Data Bindings) ---
  def __getitem__(self, key):
    return self._row[key]

  def __setitem__(self, key, value):
    self._row[key] = value

# In Form1 code

def _flatten_trade_dto(self, nested_dto: str, quantity:int=1)->list:
  """
    Takes a single nested DiagonalPutSpread DTO (the list with 1 item for open and 2 items for roll)
    and returns a flat list of 2 or 4 standardized leg dictionaries 
    for the Repeating Panel.
    """
  if not nested_dto or not isinstance(nested_dto, list) or not nested_dto[0]:
    return []

  spread_to_open_dto = nested_dto[0]
  short_leg_to_open_dto = spread_to_open_dto['short_put']
  long_leg_to_open_dto = spread_to_open_dto['long_put']
  
  # 1. Define the opening legs (the only two legs that exist in the DTO)
  first_legs_list = [
    {'action': config.ACTION_SELL_TO_OPEN, 
     'type': short_leg_to_open_dto['option_type'], 
     'strike': short_leg_to_open_dto['strike'], 
     'expiration': short_leg_to_open_dto['expiration_date'], 
     'quantity': quantity},
    {'action': config.ACTION_BUY_TO_OPEN, 
     'type': long_leg_to_open_dto['option_type'], 
     'strike': long_leg_to_open_dto['strike'], 
     'expiration': long_leg_to_open_dto['expiration_date'], 
     'quantity': quantity}
  ]
  if len(nested_dto) == 2:  #its a roll, so do the closing legs
    spread_to_close_dto = nested_dto[1] 
    short_leg_to_close_dto = spread_to_close_dto['short_put']
    long_leg_to_close_dto = spread_to_close_dto['long_put']
    second_legs_list = ([
      {'action': config.ACTION_BUY_TO_CLOSE, 
       'type': short_leg_to_close_dto['option_type'], 
       'strike': short_leg_to_close_dto['strike'], 
       'expiration': short_leg_to_close_dto['expiration_date'], 
       'quantity': quantity},
      {'action': config.ACTION_SELL_TO_CLOSE, 
       'type': long_leg_to_close_dto['option_type'], 
       'strike': long_leg_to_close_dto['strike'], 
       'expiration': long_leg_to_close_dto['expiration_date'], 
       'quantity': quantity}
    ])
    # rolling so return both spreads
    return second_legs_list + first_legs_list
  # Not a roll - return the first spread open
  return first_legs_list