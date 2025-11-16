from ._anvil_designer import RowTemplate_TradeHistoryTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class RowTemplate_TradeHistory(RowTemplate_TradeHistoryTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    if self.item:
      #print(f"short_expiry is {self.item.get('short_expiry')}")
      self.label_underlying.text = f"{self.item['Underlying']} {self.item.get('short_strike')}/{self.item.get('long_strike')}"
      self.label_strategy.text = self.item['Strategy']
