from ._anvil_designer import Form1Template
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
from .Form_ConfirmTrade import Form_ConfirmTrade # Import your custom form
from .. import config
from datetime import date


class Form1(Form1Template):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # Any code you write here will run before the form opens.
    # Log into default environment as displayed in the dropdown
    self.dropdown_environment_change()
    
    # Load data for the open positions grid
    open_trades_data = anvil.server.call('get_open_trades')
    #print("Data received on the client:", open_trades_data)  # <-- ADD THIS LINE
    self.repeatingpanel_open_positions.items = open_trades_data
    
    # Load data for the trade history grid
    self.repeatingpanel_trade_history.items = anvil.server.call('get_closed_trades')

    # Trade ticket init
    self.best_trade_dto = None # the selected position returned by find_new_diagonal_trade
    self.trade_preview_data = None # A place to store the server data
    self.button_place_trade.enabled = False
    self.label_quote_status.text = "Enter quantity and review trade."

  def dropdown_environment_change(self, **event_args):
    """This method is called when an item is selected"""
    selected_env = self.dropdown_environment.selected_value
    profile_details = anvil.server.call('get_tradier_profile', environment=selected_env)
    if profile_details:
      account_number = profile_details['account_number']
      nickname = anvil.server.call('get_account_nickname', account_number)
      self.label_login.text = f"{account_number} - {nickname}"
    else:
      self.label_login.text = "Failed to load profile"
    
  def button_tab_trade_history_click(self, **event_args):
    """This method is called when the button is clicked"""
    # Hide the open positions card and show the history card
    self.card_open_positions.visible = False
    self.card_trade_history.visible = True
  
    # Update the button appearance to show which tab is active
    self.button_tab_open_positions.role = 'outlined-button'
    self.button_tab_trade_history.role = 'filled-button'

  def button_tab_open_positions_click(self, **event_args):
    """This method is called when the button is clicked"""
    # Show the open positions card and hide the history card
    self.card_open_positions.visible = True
    self.card_trade_history.visible = False
  
    # Update the button appearance
    self.button_tab_open_positions.role = 'filled-button'
    self.button_tab_trade_history.role = 'outlined-button'

  def button_find_new_trade_click(self, **event_args):
    """This method is called when the button is clicked"""
    symbol = self.textbox_symbol.text
    if symbol is None:
      alert("must select symbol")
      return
    self.label_symbol.text = symbol
    environment = self.dropdown_environment.selected_value
    self.label_quote_status.text = "Getting underlying price..."
    underlying_price = anvil.server.call('get_underlying_quote', environment, symbol) 
    if underlying_price is None:
      alert("unable to get underlying price")
    self.label_underlying_price.text = f"{underlying_price:.2f}"
    self.label_trade_ticket_title.text = f"{self.label_trade_ticket_title.text} \
                                          - Open {self.dropdown_strategy_picker.selected_value}"
    #pop the trade entry card and gather data
    self.button_place_trade.enabled = False
    self.card_trade_entry.visible = True   
    
    # get type of trade from strategy drop down
    trade_strategy = self.dropdown_strategy_picker.selected_value
    try:
        
      if trade_strategy == 'diagonal put spread':
        self.label_quote_status.text = "Getting best trade..."
        print("calling find_new_diagonal_trade")
        best_trade_dto = anvil.server.call('find_new_diagonal_trade',
                                           self.dropdown_environment.selected_value,
                                           symbol)
      elif trade_strategy == 'cash secured put':
        alert("strategy not implemented")

      # Check if the server call was successful
      if best_trade_dto:
        print(f"best put diag DTO is: {best_trade_dto}")

        # 4. Store the best trade DTO (the dictionary)
        self.best_trade_dto = best_trade_dto

        # 5. Populate strategy leg fields
      
        # Short Leg
        short_leg = best_trade_dto['short_put']
        self.label_leg1_action.text = "sell to open"
        short_expiry = short_leg['expiration_date']
        short_dte = short_expiry - date.today()
        self.label_leg1_details.text = (
          f"Symbol: {short_leg['symbol']}, "
          f"Strike: {short_leg['strike']}, "
          f"Expiry: {short_expiry.strftime('%Y-%m-%d')}, "
          f"DTE: {short_dte.days}"
        )

        # Long Leg
        long_leg = best_trade_dto['long_put']
        self.label_leg2_action.text = "buy to open"
        long_expiry = long_leg['expiration_date']
        long_dte = long_expiry - date.today()
        self.label_leg2_details.text = (
          f"Symbol: {long_leg['symbol']}, "
          f"Strike: {long_leg['strike']}, "
          f"Expiry: {long_leg['expiration_date'].strftime('%Y-%m-%d')}, "
          f"DTE: {long_dte.days}"
        )
        net_premium = best_trade_dto['net_premium']
        self.textbox_net_credit.text = f"{net_premium:.2f}"
        self.label_spread_credit_debit.text = "credit" if net_premium >=0 else "debit"
        self.label_margin.text = f"Margin:{best_trade_dto['margin']:.2f}"
        rom_calc = best_trade_dto['ROM_rate'] * best_trade_dto['short_put']['contract_size']
        self.label_rrom.text = f"{rom_calc:.2%}"
        # --- END OF MODIFIED SECTION ---

      else:
        # Handle the case where the server didn't find a trade
        self.label_quote_status.text = "No suitable trade found."

    except Exception as e:
      self.label_quote_status.text = f"Error: {e}"
      self.label_quote_status.foreground = "error"
      
    self.label_quote_status.text = "Best trade identified"

  def button_preview_trade_click(self, **event_args):
    """Fired when the 'Preview Trade' button is clicked."""
    # get override price if selected
    if (self.textbox_overide_price.text and
      self.textbox_overide_price.text.isnumeric()):
      price = self.textbox_overide_price.text
    else:
      price = self.textbox_net_credit.text

    # 2. Get quantity from the UI
    #print(f"quantity is: {self.textbox_quantity.text}")
    quantity = int(self.textbox_quantity.text)
            
    # submit order with preview = true
    anvil.server.call('submit_order',
                     self.dropdown_environment.selected_value,
                     self.textbox_symbol.text,
                     self.b)

    # handle return dict

 
  def button_override_price_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.textbox_overide_price.visible=True
  