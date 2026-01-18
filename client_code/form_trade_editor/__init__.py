from ._anvil_designer import form_trade_editorTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
from ..form_trade_detail_card import form_trade_detail_card


class form_trade_editor(form_trade_editorTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.refresh_data()
    self.repeating_panel_trades.set_event_handler('x-trade_edit_requested', self.handle_edit)

  def refresh_data(self):
    #print('in trade editor refresh')
    self.repeating_panel_trades.items = anvil.server.call('get_all_trades_for_editor')
    #print(f'trades items: {self.repeating_panel_trades.items}')

  def handle_edit(self, trade: dict, **event_args) -> None:
    # 1. Create the card component
    detail_card = form_trade_detail_card(trade)

    # 2. Define the Alert with custom buttons
    # Buttons return their 'value' (True/False/String) when clicked
    save_clicked = alert(
      content=detail_card,
      title=f"Edit {trade['role']} Trade",
      large=True,
      buttons=[
        ("Save Changes", "save", "primary"),
        ("Settle/Close", "close", "success"),
        ("Delete Entirely", "delete", "danger"),
        ("Cancel", "cancel")
      ]
    )

    # 3. Handle the actions
    if save_clicked == "save":
      updates = detail_card.get_updates()
      anvil.server.call('crud_update_trade_fields', trade['id'], updates)
      self.refresh_data()

    elif save_clicked == "close":
      # Settlement needs a price - we can reuse the same pattern 
      # or add an 'Exit Price' box directly to the detail_card
      p_box = TextBox(type='number', placeholder="Final Exit Price")
      if alert(p_box, title="Enter Final Exit Price"):
        anvil.server.call('crud_settle_trade_manual', trade['id'], p_box.text)
        self.refresh_data()

    elif save_clicked == "delete":
      if confirm("Are you sure? This deletes all transaction history for this trade."):
        anvil.server.call('crud_delete_trade', trade['id'])
        self.refresh_data()
'''
  def handle_delete(self, trade, **event_args):
    # 'item' is the dictionary for the specific row
    #print(f"trade for delete is :{trade}")
    if confirm(f"Delete {trade['role']} trade? This cannot be undone."):
      success = anvil.server.call('crud_delete_trade', trade['id'])
      if success:
        self.refresh_data()
      else:
        alert("Delete failed")

  def handle_close(self, trade: dict, **event_args) -> None:
    # 1. Create a text box for the exit price
    price_box = TextBox(type='number', placeholder="0.00")

    # 2. Show alert to get the price
    result = alert(
      content=price_box,
      title=f"Manual Close: {trade['role']} @ {trade['symbol']}",
      buttons=[("Confirm Close", True), ("Cancel", False)]
    )

    # 3. If confirmed, call server
    if result:
      exit_px = price_box.text
      if exit_px is not None:
        success = anvil.server.call('crud_settle_trade_manual', trade['id'], exit_px)
        if success:
          Notification("Trade settled in DB.").show()
          self.refresh_data()
        else:
          alert("Settlement failed. Trade might be already closed.")
      else:
        alert("Error: You must enter an exit price.")
'''     