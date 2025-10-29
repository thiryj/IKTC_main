from ._anvil_designer import Form_ConfirmTradeTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class Form_ConfirmTrade(Form_ConfirmTradeTemplate):
  def __init__(self, trade_details, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    # Store the trade details in the form so we can use it later
    self.trade_info = trade_details

    # Use the passed-in dictionary to set the label text
    self.label_underlying.text = f"Underlying: {self.trade_info['underlying']}"
    self.label_strategy.text = f"Strategy: {self.trade_info['strategy']}"

    # Calculate net credit for display
    short_price = self.trade_info['short_leg']['price']
    long_price = self.trade_info['long_leg']['price']
    net_credit = short_price - long_price
    self.label_net_credit.text = f"Net Credit: ${net_credit:.2f}"

    # You can format the leg labels for clarity
    short_leg_text = (f"Sell to Open: {self.trade_info['short_leg']['strike']}P "
                      f"@ {self.trade_info['short_leg']['expiry']}")
    self.label_short_leg.text = short_leg_text

    long_leg_text = (f"Buy to Open: {self.trade_info['long_leg']['strike']}P "
                     f"@ {self.trade_info['long_leg']['expiry']}")
    self.label_long_leg.text = long_leg_text

  def button_confirm_click(self, **event_args):
    """This method is called when the button is clicked"""
    # Close the alert and return True
    self.raise_event("x-close-alert", value=True)

  def button_cancel_click(self, **event_args):
    """This method is called when the button is clicked"""
    # Close the alert and return False
    self.raise_event("x-close-alert", value=False)
