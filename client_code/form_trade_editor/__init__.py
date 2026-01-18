from ._anvil_designer import form_trade_editorTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class form_trade_editor(form_trade_editorTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.refresh_data()

  def refresh_data(self):
    print('in trade editor refresh')
    self.repeating_panel_trades.items = anvil.server.call('get_all_trades_for_editor')
    #print(f'trades items: {self.repeating_panel_trades.items}')

  def button_delete_click(self, **event_args):
    # 'item' is the dictionary for the specific row
    trade = event_args['sender'].item
    if confirm(f"Delete {trade['role']} trade? This cannot be undone."):
      success = anvil.server.call('crud_delete_trade', trade['id'])
      if success:
        self.refresh_data()
      else:
        alert("Delete failed")