from ._anvil_designer import RowTemplate_OpenPositionsTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class RowTemplate_OpenPositions(RowTemplate_OpenPositionsTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)

    # --- 1. Your existing code to set labels ---
    if self.item:
      #print(f"short_expiry is {self.item.get('short_expiry')}")
      self.label_underlying.text = f"{self.item['Underlying']} {self.item.get('short_strike')}/{self.item.get('long_strike')}"
      self.label_strategy.text = self.item['Strategy']
      if self.item['OpenDate']:
        se = self.item.get('short_expiry')
        se_str = f"{se:%d-%b}" if se else "-"
        self.label_open_date.text = f"{self.item['OpenDate']:%d-%b} / {se_str}"

    # --- 2. NEW RISK INDICATOR LOGIC ---
      extrinsic_val = self.item.get('extrinsic_value')
      is_at_risk = self.item.get('is_at_risk', False) # Default to False

      if extrinsic_val is not None:
        # Show the label and set its text
        self.label_assignment_risk.visible = True
        self.label_assignment_risk.text = f"Extrinsic: ${extrinsic_val:.2f}"
      else:
        # Hide the label if we don't have data
        self.label_assignment_risk.visible = False

        # --- 3. This is your "Flashing Red Button" idea ---
      if is_at_risk:
        # Set the label's color to red
        self.label_assignment_risk.foreground = 'red'
        self.button_roll_live.background = 'theme:Error'
      else:
        # Reset to default colors
        self.label_assignment_risk.foreground = None
        self.button_roll_live.background = None

      # RROC portion
      rroc = self.item.get('rroc')
      self.label_open_rroc.text = rroc
      if self.item.get('is_harvestable'):
        self.label_open_rroc.foreground = 'green'
        self.button_open_position_close.foreground = 'green'

  def button_edit_click(self, **event_args):
    """
      Called when the 'Edit' button on this row is clicked.
      """
    # 'self.item' is the enriched DTO for this row.
    # We need the original database row, which we stored in it.
    trade_to_close = self.item['trade_row']
  
    # 'self.parent' is the repeating panel.
    # We raise a custom event on it, passing the trade to be closed.
    self.parent.raise_event('x-manual-edit-requested',
                            trade=trade_to_close,
                            action_type='Edit')

  def button_roll_live_click(self, **event_args):
    """
      Called when the 'Roll (Live)' button on this row is clicked.
      """
  
    # 'self.item' is the enriched DTO for this row.
    # We need the original database row, which we stored in it.
    trade_to_roll = self.item['trade_row']
  
    # 'self.parent' is the repeating panel.
    # We raise a custom event on it, passing the trade to be closed.
    self.parent.raise_event('x-roll-trade-requested',
                            trade=trade_to_roll,
                            action_type='Roll: Spread')

  def button_open_position_close_click(self, **event_args):
    """This method is called when the button is clicked"""
    trade_to_close = self.item['trade_row']
    self.parent.raise_event('x-close-trade-requested',
                            trade=trade_to_close,
                            action_type='Close')
          
