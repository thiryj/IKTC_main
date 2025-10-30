from ._anvil_designer import Form1Template
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
from .Form_ConfirmTrade import Form_ConfirmTrade # Import your custom form


class Form1(Form1Template):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # Any code you write here will run before the form opens.
    # Load data for the open positions grid
    open_trades_data = anvil.server.call('get_open_trades')
    #print("Data received on the client:", open_trades_data)  # <-- ADD THIS LINE
    self.repeatingpanel_open_positions.items = open_trades_data
    
    
    # Load data for the trade history grid
    self.repeatingpanel_trade_history.items = anvil.server.call('get_closed_trades')
    
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
    
    # Call the server function to get the trade
    new_trade_details = anvil.server.call('find_new_diagonal_trade')

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
  
      # If the user clicked the "Confirm Trade" button (which returns True)
      if user_confirmed:
        # We will add the code to place the trade here in the next step
        print("User confirmed the trade. Placing now...")
        alert("Trade has been submitted!") # Placeholder feedback
      else:
        print("User cancelled the trade.")

  def button_login_click(self, **event_args):
    """This method is called when the button is clicked"""
    selected_env = self.dropdown_environment.selected_value
    profile_details = anvil.server.call('get_tradier_profile', environment=selected_env)
    if profile_details:
      self.label_login.text = profile_details['account_number']
    else:
      self.label_login.text = "Failed to load profile"
