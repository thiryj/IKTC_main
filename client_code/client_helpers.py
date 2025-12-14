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

from shared import config

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

def _flatten_trade_dto(self, nested_dto: str, quantity:int=1)->list:
  """
    Takes a single nested DiagonalPutSpread DTO (the list with 1 item for open and 2 items for roll)
    and returns a flat list of 2 or 4 standardized leg dictionaries 
    for the Repeating Panel.
    """
  if not nested_dto or not isinstance(nested_dto, list) or not nested_dto[0]:
    return []
  
  # 1. Define the legs of the first spread (the only two legs that exist in the DTO)
  first_legs_list = [
    {'type': nested_dto[0]['short_put']['option_type'], 
     'strike': nested_dto[0]['short_put']['strike'], 
     'expiration': nested_dto[0]['short_put']['expiration_date'], 
     'quantity': quantity},
    {'type': nested_dto[0]['long_put']['option_type'], 
     'strike': nested_dto[0]['long_put']['strike'], 
     'expiration': nested_dto[0]['long_put']['expiration_date'], 
     'quantity': quantity}
  ]
  
  if nested_dto[0]['spread_action'] == config.TRADE_ACTION_OPEN:
    first_legs_list[0]['action'] = config.ACTION_SELL_TO_OPEN
    first_legs_list[1]['action'] = config.ACTION_BUY_TO_OPEN
  elif nested_dto[0]['spread_action'] == config.TRADE_ACTION_CLOSE:
    first_legs_list[0]['action'] = config.ACTION_BUY_TO_CLOSE
    first_legs_list[1]['action'] = config.ACTION_SELL_TO_CLOSE
    
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

def handle_manual_qty_change(form_instance, **event_args):
  """
  Handles the custom event from a row.
  If a Short leg quantity is changed, update the corresponding Long leg.
  """
  row_sender = event_args.get('row_item')
  if not row_sender:
    return

  try:
    new_qty = int(row_sender.textbox_manual_leg_quantity.text)
  except (ValueError, TypeError):
    return

  source_action = row_sender.dropdown_manual_leg_action.selected_value

  # Define the mapping
  target_action = None
  if source_action == config.ACTION_SELL_TO_OPEN:
    target_action = config.ACTION_BUY_TO_OPEN
  elif source_action == config.ACTION_BUY_TO_CLOSE:
    target_action = config.ACTION_SELL_TO_CLOSE

  # Apply to siblings
  if target_action:
    # We access the repeating panel via the passed form_instance
    for row in form_instance.repeatingpanel_manual_legs.get_components():
      if row is not row_sender:
        if row.dropdown_manual_leg_action.selected_value == target_action:
          row.textbox_manual_leg_quantity.text = new_qty

def handle_manual_date_change(form_instance, **event_args):
  """Syncs expiration dates between Short and Long legs."""
  row_sender = event_args.get('row_item')
  if not row_sender: return

  new_date = row_sender.datepicker_manual_leg_expiration.date
  source_action = row_sender.dropdown_manual_leg_action.selected_value

  target_action = None
  if source_action == config.ACTION_SELL_TO_OPEN:
    target_action = config.ACTION_BUY_TO_OPEN
  elif source_action == config.ACTION_BUY_TO_CLOSE:
    target_action = config.ACTION_SELL_TO_CLOSE

  if target_action:
    for row in form_instance.repeatingpanel_manual_legs.get_components():
      if row is not row_sender:
        if row.dropdown_manual_leg_action.selected_value == target_action:
          row.datepicker_manual_leg_expiration.date = new_date
