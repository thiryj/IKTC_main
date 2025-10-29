from ._anvil_designer import RowTemplate_OpenPositionsTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class RowTemplate_OpenPositions(RowTemplate_OpenPositionsTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # The 'self.item' property is automatically populated by the Repeating Panel
    # with the data for this specific row (one of your 'Trades' table rows).
  
    self.label_underlying.text = self.item['Underlying']
    self.label_strategy.text = self.item['Strategy']
  
    # Date objects need to be formatted into a string to be displayed
    if self.item['OpenDate']:
      self.label_open_date.text = self.item['OpenDate'].strftime("%Y-%m-%d")
