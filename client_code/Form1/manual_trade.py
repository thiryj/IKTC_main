import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
from anvil import alert

# Private libs
from .. import config

# This is a module.
# You can define variables and functions here, and use them from any form. For example, in a top-level form:
#
#    from ..Form1 import Module1
#
#    Module1.say_hello()
#

def new_leg_builder(form_instance, selected_type: str, quantity: int=1):
  print("new leg builder ")

  if selected_type == config.POSITION_TYPE_DIAGONAL:
    leg_definitions = [
      {'action': 'Sell to Open', 'type': config.OPTION_TYPE_PUT, 'quantity': quantity},
      {'action': 'Buy to Open', 'type': config.OPTION_TYPE_PUT, 'quantity': quantity}
    ]
  elif selected_type == config.POSITION_TYPE_CSP:
    leg_definitions = [
      {'action': 'Sell to Open', 'type': config.OPTION_TYPE_PUT, 'quantity': quantity}
    ]
  elif selected_type == config.POSITION_TYPE_COVERED_CALL:
    leg_definitions = [
      {'action': 'Sell to Open', 'type': config.OPTION_TYPE_CALL, 'quantity': quantity}
    ]
  elif selected_type == config.POSITION_TYPE_STOCK:
    alert("stock not yet implemented - use manual db entry")
  elif selected_type == config.POSITION_TYPE_MISC:
    leg_definitions = [
      {}
    ]
  elif selected_type == 'Roll: Diagonal':
    leg_definitions = [
      {'action': 'Buy to Close', 'type': 'Put', 'quantity': quantity},
      {'action': 'Sell to Close', 'type': 'Put', 'quantity': quantity},
      {'action': 'Sell to Open', 'type': 'Put', 'quantity': quantity},
      {'action': 'Buy to Open', 'type': 'Put', 'quantity': quantity}
    ]

  elif selected_type == 'Close: Diagonal':
    leg_definitions = [
      {'action': 'Buy to Close', 'type': 'Put', 'quantity': quantity},
      {'action': 'Sell to Close', 'type': 'Put', 'quantity': quantity}
    ]

  elif selected_type == 'Roll: Leg':
    leg_definitions = [
      {'action': 'Buy to Close', 'type': 'Put', 'quantity': quantity},
      {'action': 'Sell to Open', 'type': 'Put', 'quantity': quantity}
    ]
  else:  #no valid choice selected
    
    return
  if leg_definitions:
    form_instance.repeatingpanel_manual_legs.items = leg_definitions
    form_instance.repeatingpanel_manual_legs.visible = True

def show_position_selector():
  print("position selector")

def show_stock_picker():
  print("stock picker")

'''
def manual_transaction_type_change(form_instance, action: str):
  # I don't think this code is ever called
  # called when dropdown_manual_transaction_type changed
  selected_type = form_instance.dropdown_manual_transaction_type.selected_value
  selected_action = action
  print(f"selected type: {selected_type}, selected action: {selected_action}")
  
  # Check if the selected type implies a new trade
  if selected_action and selected_action in config.NEW_TRADE_ACTIONS:
    # Show fields for a NEW trade
    form_instance.textbox_manual_underlying.visible = True
    form_instance.dropdown_manual_existing_trade.visible = False
  else:
    # Show fields for an EXISTING trade (close or roll)
    form_instance.textbox_manual_underlying.visible = False
    form_instance.dropdown_manual_existing_trade.visible = True
    
    # This will be the list of dictionaries for the repeating panel
    leg_definitions = []
    if (selected_type == config.POSITION_TYPE_DIAGONAL and selected_action == config.TRADE_ACTION_CLOSE):
      # show position selector only
      form_instance.repeatingpanel_manual_legs.visible = True
      """
      leg_definitions = [
        {'action': 'Buy to Close'},
        {'action': 'Sell to Close'}
      ]
    """
  # Now, assign this list to the repeating panel
  if leg_definitions:
    form_instance.repeatingpanel_manual_legs.items = leg_definitions
    form_instance.repeatingpanel_manual_legs.visible = True
  else:
    # Hide the panel if no legs are needed
    form_instance.repeatingpanel_manual_legs.items = []
    form_instance.repeatingpanel_manual_legs.visible = False

  form_instance.button_save_manual_trade.enabled=True
'''
