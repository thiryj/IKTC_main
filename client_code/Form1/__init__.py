from ._anvil_designer import Form1Template
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
from .Form_ConfirmTrade import Form_ConfirmTrade # Import your custom form
from .. import config


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
    self.label_symbol.text = symbol
    environment = self.dropdown_environment.selected_value
    underlying_price = anvil.server.call('get_underlying_quote', environment, symbol) 
    self.label_underlying_price.text = f"{underlying_price}"
    
    #pop the trade entry card and gather data
    self.card_trade_entry.visible = True
    
  def button_preview_trade_click(self, **event_args):
    """Fired when the 'Preview Trade' button is clicked."""
    # 1. Show loading state
    self.label_quote_status.text = "Getting quote..."
    self.button_place_trade.enabled = False
    
    # get type of trade from stratey drop down
    trade_strategy = self.dropdown_strategy_picker.selected_value

    # get symbol from input field
    symbol = self.textbox_symbol.text
    
    try:
      # 2. Get quantity from the UI
      print(f"quantity is: {self.textbox_quantity.text}")
      quantity = int(self.textbox_quantity.text)
        
      # 3. Call the server function
      if trade_strategy == 'diagonal put spread':
        preview = anvil.server.call('find_new_diagonal_trade', 
                                              self.dropdown_environment.selected_value,
                                              symbol)
      elif trade_strategy == 'cash secured put':
        alert("strategy not implemented")

      print(f"preview is: {preview}")
      price_override = self.textbox_net_credit.text
  
      # 4. Store the data
      self.trade_preview_data = preview # Save the whole dictionary
  
      # 5. Populate all the UI fields
      self.label_symbol.text = preview['symbol']
      self.label_underlying_price.text = f"@{preview['underlying_price']:.2f}"
  
      # Populate legs (assuming a 2-leg spread)
      self.label_leg1_action.text = preview['legs'][0]['action']
      self.label_leg1_details.text = preview['legs'][0]['details']
      self.label_leg2_action.text = preview['legs'][1]['action']
      self.label_leg2_details.text = preview['legs'][1]['details']
  
      # This is key: Fill the price box with the server's suggested price
      if not price_override:
        self.textbox_net_credit.text = f"{preview['net_credit']:.2f}"
  
      self.label_rom.text = f"{preview['rom'] * 100:.1f}%"
  
      # 6. Update status based on validity
      if preview['is_valid']:
        self.label_quote_status.text = "Quote is valid."
        self.label_quote_status.foreground = "primary"
        self.button_place_trade.enabled = True
      else:
        self.label_quote_status.text = "Quote is stale or invalid."
        self.label_quote_status.foreground = "secondary"
  
    except Exception as e:
      self.label_quote_status.text = f"Error: {e}"
      self.label_quote_status.foreground = "error"
  
    
  
      """
      # Check if a trade was actually found
      if new_trade_details:
        # Create an instance of our custom form, passing the trade details to it
        confirmation_form = Form_ConfirmTrade(trade_details=new_trade_details)
  
        # Show the form as a pop-up alert. The 'buttons=[]' argument removes the default OK button.
        # The alert function will return True or False, based on what we coded in the form's buttons.
        user_confirmed = alert(
          content=confirmation_form,
          title="Confirm New Trade",
          large=True,
          buttons=[]
        )
        """
