# In form_trade_detail_card
import datetime as dt
from ._anvil_designer import form_trade_detail_cardTemplate
import anvil
import re

class form_trade_detail_card(form_trade_detail_cardTemplate):
  def __init__(self, trade: dict, **properties):
    self.init_components(**properties)
    self.trade_id = trade['id']

    # Use full names matching the server dict
    self.label_role.text = trade.get('role', 'N/A')
    self.label_symbol.text = trade.get('symbol', 'N/A')

    self.text_box_qty.text = trade.get('quantity', 0)
    self.date_picker_entry.date = trade.get('entry_time')
    self.text_box_entry_price.text = trade.get('entry_price', 0)
    self.text_box_target_harvest.text = trade.get('target_harvest_price')
    self.text_box_roll_trigger.text = trade.get('roll_trigger_price')
    self.text_box_exit_price.text = trade.get('exit_price')
    self.date_picker_exit.date = trade.get('exit_time') # NEW PICKER
    self.text_box_notes.text = trade.get('notes', "")
    self.check_exclude_from_stats.checked = trade.get('exclude_from_stats', False)

    self.check_settle_validity()

  def check_settle_validity(self) -> None:
    """Enables Settle button only if Exit Price is populated."""
    val = self.text_box_exit_price.text
    try:
      float(val)
      is_number = True
    except (TypeError, ValueError):
      is_number = False

    self.button_settle.enabled = is_number

  def get_all_data(self) -> dict:
    return {
      'quantity': self.text_box_qty.text,
      'entry_time': self.date_picker_entry.date,
      'entry_price': self.text_box_entry_price.text,
      'target_harvest_price': self.text_box_target_harvest.text,
      'roll_trigger_price': self.text_box_roll_trigger.text,
      'exit_price': self.text_box_exit_price.text,
      'exit_time': self.date_picker_exit.date,
      'notes': self.text_box_notes.text,
      'exclude_from_stats': self.check_exclude_from_stats.checked
    }

  # Button Handlers - Raise x-close-alert to close the parent alert()
  @anvil.handle("button_save", "click")
  def button_save_click(self, **event_args) -> None:
    self.raise_event("x-close-alert", value="save")

  @anvil.handle("button_settle", "click")
  def button_settle_click(self, **event_args) -> None:
    self.raise_event("x-close-alert", value="settle")

  @anvil.handle("button_delete", "click")
  def button_delete_click(self, **event_args) -> None:
    self.raise_event("x-close-alert", value="delete")

  @anvil.handle("text_box_exit_price", "pressed_enter")
  def text_box_exit_price_pressed_enter(self, **event_args):
    """This method is called when the user presses Enter in this text box"""
    self.check_settle_validity()

  @anvil.handle("text_box_exit_price", "change")
  def text_box_exit_price_change(self, **event_args):
    """This method is called when the text in this text box is edited"""
    self.check_settle_validity()

  @anvil.handle("check_exclude_from_stats", "change")
  def check_exclude_from_stats_change(self, **event_args):
    """This method is called when this checkbox is checked or unchecked"""
    pass
