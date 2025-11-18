from ._anvil_designer import Form_ManualLegEntryTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables

from .. import config


class Form_ManualLegEntry(Form_ManualLegEntryTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # set action drop down
    self.dropdown_manual_leg_action.items = [config.ACTION_SELL_TO_OPEN,
                                             config.ACTION_BUY_TO_OPEN,
                                             config.ACTION_BUY_TO_CLOSE, 
                                             config.ACTION_SELL_TO_CLOSE]

    # self.item will be the dictionary, e.g.,
    # {'action': 'Buy to Close', 'type': 'Put', 'strike': 247, ...}

    if self.item:
      # Pre-fill all the component values from the item dictionary
      self.dropdown_manual_leg_action.selected_value = self.item.get('action')
      self.dropdown_manual_leg_type.selected_value = self.item.get('type', 'Put')
      self.textbox_manual_leg_quantity.text = self.item.get('quantity')
      self.textbox_manual_leg_strike.text = self.item.get('strike')
      self.datepicker_manual_leg_expiration.date = self.item.get('expiration')
      
