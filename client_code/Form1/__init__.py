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
    self.preview_data = None
    # Store the ID of the order we are tracking
    self.pending_order_id = None
    # Disable the timer initially
    self.timer_order_status.enabled = False

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

      else:
        # Handle the case where the server didn't find a trade
        self.label_quote_status.text = "No suitable trade found."

    except Exception as e:
      self.label_quote_status.text = f"Error: {e}"
      self.label_quote_status.foreground = "error"
      
    self.label_quote_status.text = "Best trade identified"
    self.button_preview_trade.enabled = True

  def button_preview_trade_click(self, **event_args):
    """Fired when the 'Preview Trade' button is clicked."""
    override_price_text = self.textbox_overide_price.text
  
    # First, check if the textbox is not empty
    if override_price_text:
      try:
        limit_price = float(override_price_text)    
      except ValueError:        
        alert("Please enter a valid number for the override price.")
    else:
      limit_price = self.textbox_net_credit.text
    
    self.preview_data = self.common_trade_button(preview=True, limit_price=limit_price)

    if self.preview_data and self.preview_data.get('order', {}).get('status') == 'ok':
      order_details = self.preview_data['order']
      self.label_trade_results_price.text = f"Limit Price: ${order_details['price']:.2f}"
      self.label_trade_results_phase.text = "Preview Results"
      self.label_trade_results_status.text = 'ok'
      print(f"Order preview is valid. Margin Change:{order_details['margin_change']}")
      # 2. Enable the final "Place Trade" button
      self.button_place_trade.enabled = True
      self.label_quote_status.text = "Preview successful. Ready to submit."
    
    else:
      # Preview failed or returned an error
      self.button_place_trade.enabled = False
      self.label_quote_status.text = "Error in trade preview. Cannot submit."
    
      # Optionally show the error message from Tradier
      if self.preview_data and self.preview_data.get('order', {}).get('errors'):
        error_msg = self.preview_data['order']['errors']['error'][0]
        alert(f"Preview failed: {error_msg}")
 
  def button_override_price_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.textbox_overide_price.visible=True

  def button_place_trade_click(self, **event_args):
    """This method is called when the Place Trade button is clicked"""
    limit_price = self.preview_data['order']['price']
    trade_response = self.common_trade_button(preview=False, limit_price=limit_price)

    if trade_response and trade_response.get('order', {}).get('status') == 'ok':
      # status of 'ok' means accepted, not filled.  there is no price yet
      self.label_trade_results_phase.text = "Trade Results"
      self.label_trade_results_status.text = 'trade submitted'

      # After submitting the order, start the timer
      if trade_response and trade_response.get('order', {}).get('status') == 'ok':
        self.pending_order_id = trade_response['order']['id']
        self.label_trade_results_status.text = f"Order {self.pending_order_id} submitted. Awaiting fill..."
        # Enable the timer toggle switch!
        self.checkbox_status_polling.enabled = True
        self.button_improve_price.enabled = True
        self.button_cancel_trade = True
      else:
        self.label_trade_results_status.text = "Order submission failed."

    else:
      # Preview failed or returned an error
      self.button_place_trade.enabled = False
      self.label_quote_status.text = "Error in trade preview. Cannot submit."

      # Optionally show the error message from Tradier
      if trade_response and trade_response.get('order', {}).get('errors'):
        error_msg = trade_response['order']['errors']['error'][0]
        alert(f"Preview failed: {error_msg}")

  def common_trade_button(self, preview: str=True, limit_price: float=None):
    """code common to both preview and trade button clicks"""
    quantity = int(self.textbox_quantity.text)
    if limit_price is None:
      print("common_trade_button must have limit price")
      return
    # set trade type.  TODO: add logic for roll later
    print("calling submit order")        
    trade_dict = anvil.server.call('submit_order',
                                     self.dropdown_environment.selected_value,
                                     self.textbox_symbol.text,
                                     self.best_trade_dto,
                                     quantity,
                                     preview=preview,
                                     limit_price=limit_price,
                                     trade_type=config.TRADE_TYPE_OPEN
                                    )

    # handle return dict
    print(f"trade response data:{trade_dict}")
    return trade_dict

  def timer_order_status_tick(self, **event_args):
    """This method is called Every [interval] seconds. Does not trigger if [interval] is 0."""
    if self.pending_order_id:
      env = self.dropdown_environment.selected_value
      status = anvil.server.call('get_order_status', env, self.pending_order_id)

      self.label_trade_results_status.text = f"Order {self.pending_order_id}: {status}"

      # Check if the order is filled or in another final state
      if status in ['filled', 'canceled', 'rejected', 'expired']:
        # Stop the timer, we're done.
        self.timer_order_status.enabled = False
        self.pending_order_id = None
        #self.label_trade_results_price.text = f"Limit Price: ${order_details['price']:.2f}"
        print("Order is in a final state. Stopping timer.")
        # You would now refresh your main positions grid

  def button_cancel_trade_click(self, **event_args):
    """This method is called when the cancel button is clicked"""
    # Check if we are actually tracking a pending order
    if self.pending_order_id:
      env = self.dropdown_environment.selected_value

      # Call the new server function
      status = anvil.server.call('cancel_order', env, self.pending_order_id)

      if status == "Order canceled":
        # Stop the timer, we're done
        self.timer_order_status.enabled = False
        self.label_order_status.text = f"Order {self.pending_order_id} was canceled."
        self.pending_order_id = None
      else:
        alert(f"Failed to cancel order: {status}")
    else:
      alert("No pending order to cancel.")

  def checkbox_status_polling_change(self, **event_args):
    """This method is called when this checkbox is checked or unchecked"""
    if self.checkbox_status_polling.checked:    
      self.timer_order_status.enabled = True
    else:
      self.timer_order_status.enabled = False
    
  