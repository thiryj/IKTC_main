from ._anvil_designer import Form_ManualLegEntryTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class Form_ManualLegEntry(Form_ManualLegEntryTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # self.item will be the dictionary, e.g., {'action': 'Sell to Open'}
    if self.item:
      # Set the dropdown's selected value
      self.dropdown_manual_leg_action.selected_value = self.item.get('action')

      # You could pre-fill quantity, type, etc. here too
      self.dropdown_manual_leg_type.selected_value = self.item.get('type', 'Put')
