from ._anvil_designer import RowTemplate1Template
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class RowTemplate1(RowTemplate1Template):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    self.label_role.text = self.item.get('role', '-')
    self.label_qty.text = self.item.get('quantity', 0)
    self.label_px.text = f"${self.item.get('entry_px', 0):.2f}"
    #self.label_harvest.text = f"${self.item['target_harvest']:.2f}" if self.item.get('target_harvest') else "-"
    self.label_time.text = self.item['entry_time'].strftime('%Y-%m-%d %H:%M') if self.item.get('entry_time') else "-"
    #self.label_trigger.text = f"${self.item['roll_trigger']:.2f}" if self.item.get('roll_trigger') else "-"

    # PnL with conditional coloring
    #pnl = self.item.get('pnl', 0)
    #self.label_pnl.text = f"${pnl:.2f}"
    #self.label_pnl.foreground = "green" if pnl >= 0 else "red"

  @handle("button_edit", "click")
  def button_edit_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.parent.raise_event('x-trade_edit_requested', trade=self.item)
    
