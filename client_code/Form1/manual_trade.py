import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
from anvil import alert

# Private libs
from ..shared import config

# This is a module.
# You can define variables and functions here, and use them from any form. For example, in a top-level form:
#
#    from ..Form1 import Module1
#
#    Module1.say_hello()
#

def new_leg_builder(form_instance, selected_type: str, quantity: int=1):
  print(f"new leg builder with strategy: {selected_type} ")

  if selected_type == config.POSITION_TYPE_VERTICAL:
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

