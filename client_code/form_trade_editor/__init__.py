from ._anvil_designer import form_trade_editorTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
from .form_trade_detail_card import form_trade_detail_card
from ..shared import config


class form_trade_editor(form_trade_editorTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.refresh_data()
    self.repeating_panel_trades.set_event_handler('x-trade_edit_requested', self.handle_edit)

  def refresh_data(self):
    #print('in trade editor refresh')
    trades = anvil.server.call('get_all_trades_for_editor')
    self.repeating_panel_trades.items = sorted(trades, 
                                               key=lambda x: (x['status'] != config.STATUS_OPEN ,
                                                              x['role'] != config.ROLE_HEDGE)
                                              )
    
    #print(f'trades items: {self.repeating_panel_trades.items}')

  def handle_edit(self, trade: dict, **event_args) -> None:
    # 1. Instantiate the card
    detail_card = form_trade_detail_card(trade)
  
    # 2. Open Alert with NO default buttons
    action = alert(
      content=detail_card, 
      title=f"Manual Edit: {trade['role']}", 
      large=True, 
      buttons=[] 
    )
  
    # 3. Action Logic
    if action == "save":
      data = detail_card.get_all_data()
      anvil.server.call('crud_update_trade_metadata', trade['id'], data)
  
    elif action == "settle":
      data = detail_card.get_all_data()
      if confirm("Settle this trade and mark as CLOSED in database?"):
        anvil.server.call('crud_settle_trade_manual', trade['id'], data)
  
    elif action == "delete":
      if confirm("PERMANENTLY DELETE trade and all history?"):
        anvil.server.call('crud_delete_trade', trade['id'])
  
      # 4. Cleanup
    self.refresh_data()