# Anvil libs
from ._anvil_designer import Form1Template
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables, Row

# Public libs
import datetime as dt

# Private libs
from .. import config, client_helpers
from . import manual_trade
from .Form_ConfirmTrade import Form_ConfirmTrade # Import your custom form
from .Form_ManualLegEntry import Form_ManualLegEntry

class Form1(Form1Template):
  def __init__(self, **properties):
    # populate settings components first
        
    # get the settings row (one row of data) and pass it to helper class
    row = anvil.server.call('get_settings')
    
    # this trick allows dot notation
    self.my_settings = client_helpers.LiveSettings(row)
    
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # Globals
    self.refresh_data_bindings()
    self.environment = config.ENV_SANDBOX
    self.trade_dto = None # the new combined var to hold 2 leg new spreads or 4 leg roll spreads
    self.trade_dto_list = []
    # {spread meta, 'short_put':{}, 'long_put':{}}
    self.manual_entry_state = None    # open or edit
    self.trade_ticket_state = None
    self.preview_data = None # dict that is returned by Preview Trade button
    self.pending_order_id = None 
    self.active_trade_row = None

    # **************Timers
    self.timer_order_status.interval = 0 # disabled for now
    self.timer_risk_refresh.interval = config.REFRESH_TIMER_INTERVAL if self.my_settings.refresh_timer_on else 0

    # ***************Events
    # Open Positions edit event broadcast
    self.repeatingpanel_open_positions.set_event_handler(
      'x-manual-edit-requested', self.handle_manual_edit_request
    )
    # Open Positions roll live event broadcast
    self.repeatingpanel_open_positions.set_event_handler(
      'x-roll-trade-requested', self.handle_roll_trade_request
    )

    # Open Positions close live event broadcast
    self.repeatingpanel_open_positions.set_event_handler(
      'x-close-trade-requested', self.handle_close_trade_request
    )

    # Manual Entry card quantity changed event broadcast
    self.repeatingpanel_manual_legs.set_event_handler(
      'x-manual-qty-change', self.on_manual_qty_change
    )
    # Manual Entry card date changed event broadcast
    self.repeatingpanel_manual_legs.set_event_handler(
      'x-manual-date-change', self.on_manual_date_change
    )
          
    # Populate misc components
    self.dropdown_strategy_picker.items = config.POSITION_TYPES_ACTIVE
    self.textbox_symbol.text = self.my_settings.default_symbol
    
    # Trade history grid
    self.load_trade_history()
    
    # Manual Trade Entry Card (records trade history into db)
    self.dropdown_manual_transaction_type.items = config.POSITION_TYPES
        
    # Trade Ticket
    self.button_place_trade.enabled = False
    self.label_quote_status.text = "Enter quantity and review trade."

    # Log into default environment as displayed in the dropdown
    self.dropdown_environment_change()

  # --- EVENT HANDLER WRAPPER METHODS ---
  def on_manual_qty_change(self, **event_args):
    """Delegates to client_helpers, passing 'self'"""
    client_helpers.handle_manual_qty_change(self, **event_args)

  def on_manual_date_change(self, **event_args):
    """Delegates to client_helpers, passing 'self'"""
    client_helpers.handle_manual_date_change(self, **event_args)
    
  def dropdown_environment_change(self, **event_args):
    """This method is called when an item is selected"""
    self.environment= self.dropdown_environment.selected_value # save to form global
    self.refresh_open_positions_grid(refresh_risk=False)
    #self.repeatingpanel_trade_history.items = anvil.server.call('get_closed_trades', self.environment)
    self.load_trade_history()
    profile_details = anvil.server.call('get_tradier_profile', environment=self.environment)
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
    #self.repeatingpanel_trade_history.items = anvil.server.call('get_closed_trades', 
    #                                                            self.dropdown_environment.selected_value)
    self.load_trade_history()
  
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
    self.active_trade_row = None  # Clear it for new trades
    symbol = self.textbox_symbol.text.upper()
    if symbol is None:
      alert("must select symbol")
      return
    self.label_symbol.text = symbol
    self.label_quote_status.text = "Getting underlying price..."
    underlying_price = anvil.server.call('get_price', self.environment, symbol) 
    if underlying_price is None:
      alert("unable to get underlying price")
    else:
      self.label_underlying_price.text = f"{underlying_price:.2f}"
    self.label_trade_ticket_title.text = f"{self.label_trade_ticket_title.text} - Open {self.dropdown_strategy_picker.selected_value}"
    self.trade_ticket_state = config.TRADE_TICKET_STATE_OPEN
    
    # get type of trade from strategy drop down
    trade_strategy = self.dropdown_strategy_picker.selected_value
    try:
      best_trade_dto = None
      # 1. Unified Server Call
      result_package = anvil.server.call('get_new_open_trade_dto', 
                                         self.environment, 
                                         symbol, 
                                         trade_strategy)

      if not result_package or not result_package.get('new_spread_dto'):
        alert("No suitable trade found.")
        self.label_quote_status.text = "Search failed."
        return

      best_trade_dto = result_package['new_spread_dto']
      
      # 2. Populate UI (Standard Logic)
      self.trade_dto = best_trade_dto
      self.trade_dto_list = [self.trade_dto]
      
      """
      if trade_strategy == config.POSITION_TYPE_VERTICAL:
        result = anvil.server.call('get_vertical_spread', 
                                   self.environment, 
                                   symbol=symbol,
                                   target_delta=config.DEFAULT_VERTICAL_DELTA,
                                   width=self.my_settings.default_width,
                                   quantity=self.my_settings.default_qty,
                                   target_rroc=self.my_settings.default_target_rroc)
        #print(f"vert_spread result: {result}")
        if result is None:
          alert("Server returned no result.")
          self.label_quote_status.text = "Search failed."
          return
        if result.get('error'):
          alert(f"Error: {result['error']}")
          return
  
        # 2. ADAPTER: Convert result to 'best_trade_dto' format
        # Parse string date to object
        exp_date = dt.datetime.strptime(result['parameters']['expiration'], '%Y-%m-%d').date()

        # Calculate ROM (Return on Margin)
        credit = result['financials']['credit_per_contract']
        margin = result['financials']['margin_per_contract']
        rom = (credit / margin) if margin > 0 else 0

        # Build the DTO your UI expects
        best_trade_dto = {
          'short_put': {
            'symbol': result['legs']['short']['symbol'],
            'strike': result['legs']['short']['strike'],
            'expiration_date': exp_date,
            'contract_size': 100
          },
          'long_put': {
            'symbol': result['legs']['long']['symbol'],
            'strike': result['legs']['long']['strike'],
            'expiration_date': exp_date # Verticals share expiry
          },
          'net_premium': credit,
          'margin': margin,
          'ROM_rate': rom,
          'spread_action': config.TRADE_ACTION_OPEN
          #'strategy_type': config.STRATEGY_VERTICAL
        }

        # Wrap it to match existing structure
        best_trade_dict = {'new_spread_dto': best_trade_dto}
      elif trade_strategy == config.POSITION_TYPE_DIAGONAL:
        self.label_quote_status.text = "Getting best diagonal..."
        print("calling get_new_open_trade_dto")
        # best_trade_dto is really a dict with 'new_spread_dto' as the payload
        best_trade_dict = anvil.server.call('get_new_open_trade_dto',
                                           self.dropdown_environment.selected_value,
                                           symbol)
      elif trade_strategy == config.POSITION_TYPE_CSP:
        alert("strategy not implemented")
      
      # Check if the server call was successful
      # extract trade_dto from dict
      if best_trade_dict:
        best_trade_dto = best_trade_dict['new_spread_dto']
      else:
        alert("No Valid open trade")
        return
      """
      
      print(f"best {trade_strategy} DTO is: {best_trade_dto}")

      # 5. Populate strategy leg fields
    
      # Short Leg
      short_leg = best_trade_dto['short_put']
      self.label_leg1_action.text = config.ACTION_SELL_TO_OPEN
      short_expiry = short_leg['expiration_date']
      short_dte = short_expiry - dt.date.today()
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
      long_dte = long_expiry - dt.date.today()
      self.label_leg2_details.text = (
        f"Symbol: {long_leg['symbol']}, "
        f"Strike: {long_leg['strike']}, "
        f"Expiry: {long_leg['expiration_date'].strftime('%Y-%m-%d')}, "
        f"DTE: {long_dte.days}"
      )
      net_premium = best_trade_dto['net_premium']
      self.textbox_net_credit.text = f"{net_premium:.2f}"
      self.label_spread_credit_debit.text = "credit" if net_premium >=0 else "debit"
      self.label_margin.text = f"Margin: {best_trade_dto['margin']:.2f}"
      rom_calc = best_trade_dto['ROM_rate'] * best_trade_dto['short_put']['contract_size']
      self.label_rrom.text = f"{rom_calc:.1%}"
      self.label_quote_status.text = "Best trade identified"
      #print(f"result_package: {result_package}")
      #print(f"result_package-quantity: {result_package.get('quantity',99)}")
      self.textbox_trade_entry_quantity.text = result_package['new_spread_dto']['quantity']
      self.common_trade_ticket()

    except Exception as e:
      self.label_quote_status.text = f"Error: {e}"
      self.label_quote_status.foreground = "error"
      alert(f"Error finding new trade: {e}")
      self.card_trade_entry.visible = True  
      
  def handle_close_trade_request(self, trade, **event_args):
    """Called by the 'Close' button in Open Positions row."""
    self.card_trade_entry.visible = True
    self.active_trade_row = trade # Store it
    print(f"Handling close request for: {trade['Underlying']}")
    
    try:
      self.label_symbol.text = trade['Underlying']
      self.label_quote_status.text = "Calculating closing cost..."
      self.label_trade_ticket_title.text = f"{self.label_trade_ticket_title.text} - Close {trade['Strategy']}"

      # 1. Fetch Close Package
      close_dto = anvil.server.call('get_close_trade_dto', self.environment, trade)

      if not close_dto:
        alert("Could not calculate closing trade details.")
        return

      # 2. Store Data & Set State
      self.trade_dto = close_dto
      self.trade_dto_list = [close_dto]
      self.trade_ticket_state = config.TRADE_TICKET_STATE_CLOSE

      # 3. Populate Ticket UI (Closing Logic)
      # Leg 1: Buy to Close the Short
      short_leg = close_dto['short_put']
      self.label_leg1_action.text = config.ACTION_BUY_TO_CLOSE
      self.label_leg1_details.text = f"{short_leg['strike']} {short_leg['option_type']} Exp: {short_leg['expiration_date']}"

      # Leg 2: Sell to Close the Long
      long_leg = close_dto['long_put']
      self.label_leg2_action.text = config.ACTION_SELL_TO_CLOSE
      self.label_leg2_details.text = f"{long_leg['strike']} {long_leg['option_type']} Exp: {long_leg['expiration_date']}"

      # 4. Set Price (Debit)
      debit_cost = close_dto['cost_to_close']  
      self.textbox_net_credit.text = f"{debit_cost:.2f}"
      self.label_spread_credit_debit.text = "debit"

      self.label_rrom.text = "ROM: N/A"
      self.label_quote_status.text = "Closing trade loaded."

      # 5. Open Ticket
      self.common_trade_ticket(trade)

    except Exception as e:
      alert(f"Error preparing close trade: {e}")
      self.card_trade_entry.visible = True
    
  def handle_roll_trade_request(self, trade, **event_args):
    """
    Called by a row's 'Roll' button.
    trade is anvil.tables.Row object of 
    Calls the server to get a full 4-leg roll package
    and pre-fills the trade ticket card.
    """
    self.active_trade_row = trade # Store it
    print(f"Handling roll request for trade: {trade['Underlying']} {trade['Strategy']}")
    try:      
      self.label_symbol.text = trade['Underlying']  
      underlying_price = anvil.server.call('get_price', self.environment, self.label_symbol.text) 
      if underlying_price is None:
        alert("unable to get underlying price")
      else:
        self.label_underlying_price.text = f"{underlying_price:.2f}"
      # 2. Call the server to get the 4-leg package
      self.label_quote_status.text = "Calculating best roll..."
      # roll_package is a dict with 3 items
      # 'legs_to_populate' is a list of 4 leg dtos
      # 'total_roll_credit' is a float 
      # 'new_spread_dto' is the full dict with meta and legs
      #current_spread_width = closing_short
      # get margin setting
      
      print(f" margin expansion limit setting: {self.my_settings.margin_expansion_limit}")
      roll_package = anvil.server.call('get_roll_package_dto', 
                                       self.environment, 
                                       trade, 
                                       self.my_settings.margin_expansion_limit
                                      )
  
      if not roll_package:
        alert("Could not find a suitable roll for this position.")
        return
  
        # 3. Store the 4-leg DTO list. We'll need this when we submit.
      current_roll_dto_list = roll_package['legs_to_populate']
      self.trade_dto_list = [roll_package.get('new_spread_dto'), roll_package.get('closing_spread_dto')]
      #print(f"handle_roll_trade_request: self.trade_dto_list:{self.trade_dto_list}")
  
      # 4. Populate the Trade Ticket UI.
      #    We will display the two *new* legs (legs 3 and 4)
      #    and the *total* net credit for the entire roll.
      # The 'to close' legs are the first two in the list
            
      # extract the legs from the list
      #closing_short = [p for p in current_roll_dto_list if p.get('action')==config.ACTION_BUY_TO_CLOSE][0]
      #closing_long = [p for p in current_roll_dto_list if p.get('action')==config.ACTION_SELL_TO_CLOSE][0]
      opening_short = [p for p in current_roll_dto_list if p.get('action')==config.ACTION_SELL_TO_OPEN][0]
      opening_long = [p for p in current_roll_dto_list if p.get('action')==config.ACTION_BUY_TO_OPEN][0]
        
      self.label_leg1_action.text = opening_short.get('action')
      self.label_leg1_details.text = (
        f"Strike: {opening_short['strike']}, "
        f"Expiry: {opening_short['expiration'].strftime('%Y-%m-%d')}"
      )
  
      self.label_leg2_action.text = opening_long.get('action')
      self.label_leg2_details.text = (
        f"Strike: {opening_long['strike']}, "
        f"Expiry: {opening_long['expiration'].strftime('%Y-%m-%d')}"
      )
  
      # Use the total calculated credit for the whole roll
      total_credit = roll_package['total_roll_credit']
      self.textbox_net_credit.text = f"{total_credit:.2f}"
  
      # (We'll skip ROM for now as it's more complex for a roll)
      self.label_rrom.text = "ROM: N/A"
  
      # 5. Show the card, ready for the user to preview/submit
  
      self.label_quote_status.text = "Roll package loaded. Ready for preview."
      self.trade_ticket_state = config.TRADE_TICKET_STATE_ROLL
      self.common_trade_ticket(trade)
  
    except Exception as e:
      alert(f"Error calculating roll: {e}")
      #self.card_trade_entry.visible = False
      self.card_trade_entry.visible = True  

  def common_trade_ticket(self, trade=None):
    """
    Called from find new trade button or Roll button or Close button.  Places live trades
    """
    #pop the trade entry card and preview
    self.button_preview_trade.enabled = True
    if trade:
      legs_list = anvil.server.call('get_active_legs_for_trade', trade, 'short')
      self.textbox_trade_entry_quantity.text = legs_list[0]['Quantity'] if legs_list else 0
    self.card_trade_entry.visible = True   
    # auto run preview trade:  why wait?
    self.button_preview_trade_click()

  def button_preview_trade_click(self, **event_args):
    """Fired when the 'Preview Trade' button is clicked."""   
    override_price_text = None
    if self.textbox_overide_price.text:
      override_price_text = self.textbox_overide_price.text
      try:
        limit_price = float(override_price_text)    
      except ValueError:        
        alert("Please enter a valid number for the override price.")
        return
    limit_price = override_price_text if override_price_text else self.textbox_net_credit.text
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
    # deal with over-ride price
    if self.textbox_overide_price.text:
      override_price_text = self.textbox_overide_price.text
      try:
        limit_price = float(override_price_text)    
      except ValueError:        
        alert("Please enter a valid number for the override price.")
        return
    else:
      if self.preview_data:
        limit_price = self.preview_data['order']['price']
      else:
        alert("Bad preview data")
        return
    
    trade_response = self.common_trade_button(preview=False, limit_price=limit_price)

    if trade_response and trade_response.get('order', {}).get('status') == 'ok':
      # status of 'ok' means accepted, not filled.  there is no price yet
      self.label_trade_results_phase.text = "Trade Results"
      self.label_trade_results_status.text = 'trade submitted'

      # After submitting the order, start the timer
      if trade_response and trade_response.get('order', {}).get('status') == 'ok':
        self.pending_order_id = trade_response['order']['id']
        self.label_trade_results_status.text = f"Order {self.pending_order_id} submitted. Awaiting fill..."
        self.label_trade_results_price.text = f"Limit Price: ${limit_price:.2f}"
        # Enable the timer toggle switch!
        self.checkbox_status_polling.enabled = True
        self.button_improve_price.enabled = True
        self.button_cancel_trade.enabled = True
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
    quantity = int(self.textbox_trade_entry_quantity.text)
    
    print(f"calling submit order with preview: {preview}")        
    trade_dict = anvil.server.call('submit_order',
                                     self.dropdown_environment.selected_value,
                                     self.textbox_symbol.text,
                                     self.trade_dto_list, # list of nested dict with {spread meta..., 'short_put', 'long_put'}
                                     quantity,
                                     preview=preview,
                                     limit_price=limit_price                    
                                    )

    # handle return dict
    print(f"trade response data:{trade_dict}")
    return trade_dict

  def button_cancel_trade_click(self, **event_args):
    """This method is called when the cancel button is clicked"""
    # Check if we are actually tracking a pending order
    if self.pending_order_id:

      # Call the new server function
      status = anvil.server.call('cancel_order', self.environment, self.pending_order_id)

      if status == "Order canceled":
        # Stop the timer, we're done
        self.timer_order_status.enabled = False
        self.label_order_status.text = f"Order {self.pending_order_id} was canceled."
        self.pending_order_id = None
      else:
        alert(f"Failed to cancel order: {status}")
    else:
      alert("No pending order to cancel.")
      
  def button_cancel_trade_ticket_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.reset_card_trade_entry()
    self.card_trade_entry.visible=False

### Manual Entry Card 

  def button_open_record_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.reset_card_manual_trade()
    self.card_manual_entry.visible = True
    self.label_manual_entry_card.text = "Manual Entry: Open (record) new position" 
    self.manual_entry_state = config.MANUAL_ENTRY_STATE_OPEN
    self.datepicker_manual_date.max_date=dt.date.today()
    self.datepicker_manual_date.date=dt.date.today()
    self.dropdown_manual_transaction_type.selected_value=config.MANUAL_ENTRY_DEFAULT_POSITION_TYPE
    self.dropdown_manual_transaction_type_change()
    self.dropdown_manual_transaction_type.visible=True
    self.textbox_manual_underlying.visible = True
    self.button_manual_delete_trade.visible = False

  def dropdown_manual_transaction_type_change(self, **event_args):
    """Shows the correct number of leg entry rows based on
    what type of manual transaction is being entered.
    """
    #manual_trade.manual_transaction_type_change(self, action=config.TRADE_ACTION_OPEN)
    selected_strategy = self.dropdown_manual_transaction_type.selected_value
    if selected_strategy in config.POSITION_TYPES:
      manual_trade.new_leg_builder(self, selected_strategy, self.my_settings.default_qty)
      self.button_save_manual_trade.enabled=True
    else:
      self.reset_card_manual_trade()
      self.dropdown_manual_transaction_type.visible = True
      self.textbox_manual_underlying.visible = True

  def button_save_manual_trade_click(self, **event_args):
    # get common elements
    trade_date = self.datepicker_manual_date.date
    try:
      net_price = float(self.textbox_manual_credit_debit.text)
      if self.manual_entry_state == config.MANUAL_ENTRY_STATE_CLOSE and net_price > 0:
        alert("closing a short postion is typically a (negative) debit")
    except (TypeError, ValueError):
      alert("Please enter a valid net credit/debit number (e.g., 1.50 or -0.25).")
      return

    # initialize local vars
    existing_trade_row = None
    selected_strategy = None   # Strategy:  Diagonal, Covered Call, CSP, Stock, Misc
    
    # branch on state
    if self.manual_entry_state == config.MANUAL_ENTRY_STATE_OPEN:
      underlying_symbol = self.textbox_manual_underlying.text
      if not underlying_symbol:
        alert("Please enter an underlying symbol for a new trade.")
        return
      selected_strategy = self.dropdown_manual_transaction_type.selected_value
    else: #the mode is CLOSE or ROLL
      existing_trade_row = self.dropdown_manual_existing_trade.selected_value
      if not existing_trade_row:
        alert("Please select an existing trade.")
        return
      trade_row_dict = dict(existing_trade_row)
      underlying_symbol = trade_row_dict.get('Underlying')
      selected_strategy= trade_row_dict.get('Strategy')
    # 2. Create a list to hold the data from each leg row
    legs_data_list = []
  
    # 3. Loop through each row in the repeating panel
    # .get_components() returns a list of the Form_ManualLegEntry instances
    for leg_row_form in self.repeatingpanel_manual_legs.get_components():
      try:
        # 4. Read the data from the components in that row
        leg_data = {
          'action': leg_row_form.dropdown_manual_leg_action.selected_value,
          'quantity': int(leg_row_form.textbox_manual_leg_quantity.text),
          'option_type': leg_row_form.dropdown_manual_leg_option_type.selected_value,
          'strike': float(leg_row_form.textbox_manual_leg_strike.text),
          'expiration': leg_row_form.datepicker_manual_leg_expiration.date,
          'underlying_symbol': underlying_symbol
        }
        if not all(leg_data.values()):
          alert("Please fill out all fields for each leg.")
          return
        # 5. Add this leg's data to our list
        legs_data_list.append(leg_data)
  
      except Exception as e:
        alert(f"Error reading leg data: {e}. Please check your inputs.")
        return # Stop processing if there's an error
        
    # validation will only work if recording legs that haven't expired
    # so make this an optional action
    if self.checkbox_manual_trade_validate.checked:
      try: # validate then save
        validation_result = anvil.server.call(
          'validate_manual_legs',
          self.environment,
          legs_data_list 
        )
  
        # 2. Check the result
        if validation_result is not True:
          # Validation failed! Stop and warn the user.
          alert(f"Validation Error: {validation_result}. Please correct and resave.")
          return # Stop the save
      except Exception as e:
        alert(f"Validation Error: {validation_result}. Please correct and resave.")
        return # Stop processing if there's an error
        
    response = anvil.server.call('save_manual_trade', 
                                  self.environment,
                                  selected_strategy, # Strategy: Diagonal, Covered Call
                                  self.manual_entry_state,  # OPEN or CLOSE or ROLL
                                  trade_date, 
                                  net_price,
                                  legs_data_list,                            
                                  existing_trade_row   # existing trade for Close/Roll (or None for Open)
                      )
    alert(response)

    # 5. Hide the card and refresh your open positions
    self.card_manual_entry.visible = False
    refresh_risk_bool = True if self.manual_entry_state in config.MANUAL_ENTRY_STATE_OPEN else False
    self.refresh_open_positions_grid(refresh_risk=refresh_risk_bool) 
    self.reset_card_manual_trade()

  def refresh_open_positions_grid(self, refresh_risk: bool=True):
    #print("Refreshing open positions with live risk data...") if refresh_risk else print("...Updating positions")     
    # Call the new "smart" function and pass the environment
    open_trades_data = anvil.server.call('get_open_trades_with_risk', self.environment, refresh_risk)
  
    self.repeatingpanel_open_positions.items = open_trades_data
    print(f"...RROC/Risk data loaded for {len(open_trades_data)} positions") if refresh_risk else print(f"...{len(open_trades_data)} Positions updated")

  def dropdown_manual_existing_trade_change(self, **event_args):
    """
      Called when the user selects an existing trade from the dropdown.
      Fetches that trade's active legs and pre-fills the legs panel.
      """
  
    # 1. Get the selected trade row (or None)
    selected_trade = self.dropdown_manual_existing_trade.selected_value
    
    if selected_trade:
      # 3. Call the server to get the active legs for this trade
      active_legs = anvil.server.call('get_active_legs_for_trade', selected_trade)
  
      # 4. Build the new list of leg definitions for the panel
      leg_definitions = []
  
      for leg in active_legs:
        # Determine the correct closing action
        closing_action = None
        if leg['Action'] == config.ACTION_SELL_TO_OPEN:
          closing_action = config.ACTION_BUY_TO_CLOSE
        elif leg['Action'] == config.ACTION_BUY_TO_OPEN:
          closing_action = config.ACTION_SELL_TO_CLOSE
        else:
          # Fallback in case the action is unknown
          closing_action = 'UNKNOWN'
  
          # This is the data we'll pass to the row template
        leg_def = {
          'action': closing_action,
          'type': leg['OptionType'],
          'strike': leg['Strike'],
          'expiration': leg['Expiration'],
          'quantity': leg['Quantity']
        }
        leg_definitions.append(leg_def)
  
        # 5. Populate the repeating panel
      self.repeatingpanel_manual_legs.items = leg_definitions
      self.repeatingpanel_manual_legs.visible = True
      self.button_manual_delete_trade.visible = True
    else:
      # No trade selected, clear the panel
      self.repeatingpanel_manual_legs.items = []
      self.repeatingpanel_manual_legs.visible = False
      self.button_manual_delete_trade.visible = False
        
  def handle_manual_edit_request(self, trade: Row, action_type: str, **event_args):
    """
      Called by a row's 'Edit (Record)' button.
      Opens the manual entry card and pre-fills it.
      """
    print(f"Handling manual entry request for: {action_type}")
  
    self.reset_card_manual_trade()
    self.label_manual_entry_card.text = "Manual Entry: Close (record) existing position"
    trade_list = anvil.server.call('get_open_trades_for_dropdown', self.environment)
    self.dropdown_manual_existing_trade.items = trade_list
    self.dropdown_manual_existing_trade.visible=True
    self.checkbox_manual_entry_roll.visible=True
        
    # 4. Pre-select the existing trade
    # Note: We must pass the full 'trade' row object, not just an ID
    self.dropdown_manual_existing_trade.selected_value = trade
    self.dropdown_manual_existing_trade_change()
  
    # 6. Show the card
    self.manual_entry_state = config.MANUAL_ENTRY_STATE_CLOSE
    self.card_manual_entry.visible = True  
    self.button_save_manual_trade.enabled = True

  def button_cancel_manual_trade_click(self, **event_args):
    """
      Called when the 'Cancel' button on the manual entry card is clicked.
      """
    self.card_manual_entry.visible = False
    self.reset_card_manual_trade()
    self.refresh_open_positions_grid(refresh_risk=False)

  def reset_card_manual_trade(self):
    """
      Resets all input components on the manual entry card to a default state.
      """
    self.dropdown_manual_transaction_type.selected_value = None
    self.dropdown_manual_transaction_type.visible = False
    self.dropdown_manual_existing_trade.selected_value = None
    self.dropdown_manual_existing_trade.visible = False
    self.textbox_manual_underlying.text = self.my_settings.default_symbol
    self.textbox_manual_credit_debit.text = None
    self.textbox_manual_underlying.visible = False
    self.checkbox_manual_entry_roll.checked=False
    self.checkbox_manual_entry_roll.visible=False
    self.datepicker_manual_date.date = dt.date.today() 
    self.repeatingpanel_manual_legs.items = []
    self.manual_entry_state = None

  def reset_card_trade_entry(self):
    """
      Resets all input components on the manual entry card to a default state.
    """
    self.label_trade_ticket_title.text = 'Trade Ticket'
    self.textbox_trade_entry_quantity.text = self.my_settings.default_qty
    self.label_margin.text = None
    self.textbox_overide_price.text = None
    self.trade_ticket_state = None

  def button_refresh_open_positions_risk_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.refresh_open_positions_grid(refresh_risk=True)

  def timer_risk_refresh_tick(self, **event_args):
    """This method is called Every [interval] seconds. Does not trigger if [interval] is 0."""
    self.refresh_open_positions_grid(refresh_risk=True)

  def checkbox_status_polling_change(self, **event_args):
    """This method is called when this checkbox is checked or unchecked"""
    if self.checkbox_status_polling.checked:    
      self.timer_order_status.enabled = True
    else:
      self.timer_order_status.enabled = False

  def timer_order_status_tick(self, **event_args):
    """This method is called Every [interval] seconds. Does not trigger if [interval] is 0."""
    if self.pending_order_id:
      status = anvil.server.call('get_order_status', self.environment, self.pending_order_id)
  
      self.label_trade_results_status.text = f"Order {self.pending_order_id}: {status}"
  
      # Check if the order is filled or in another final state
      if status in ['filled', 'canceled', 'rejected', 'expired']:
        # Stop the timer, we're done.
        self.timer_order_status.enabled = False
        self.pending_order_id = None
        #self.label_trade_results_price.text = f"Limit Price: ${order_details['price']:.2f}"
        print("Order is in a final state. Stopping timer.")
        # You would now refresh your main positions grid

  def button_card_settings_visible_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.card_settings.visible = True

  def button_card_settings_cancel_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.card_settings.visible = False

  def textbox_default_symbol_change(self, **event_args):
    # Optional: Force uppercase on symbol entry
    self.item['default_symbol'] = self.textbox_symbol.text.upper()
    
  def checkbox_refresh_timer_on_change(self, **event_args):
    """This method is called when this checkbox is checked or unchecked"""
    self.timer_risk_refresh.interval = config.REFRESH_TIMER_INTERVAL if self.my_settings.refresh_timer_on else 0

  def checkbox_manual_entry_roll_change(self, **event_args):
    """This method is called when this checkbox is checked or unchecked"""
    # add two new blank rows to the manual entry card for user to fill in for the roll to legs
    if self.checkbox_manual_entry_roll.checked:
      self.label_manual_entry_card.text = "Manual Entry: Roll (record) existing position" 
      self.manual_entry_state = config.MANUAL_ENTRY_STATE_ROLL
      
      # 1. Get the closing leg(s) that are already pre-filled
      current_legs = list(self.repeatingpanel_manual_legs.items)
      
      # 2. Define the new blank 'opening' leg(s)
      new_opening_legs = []
      for closing_leg in current_legs:
        new_leg = closing_leg.copy()
        # Converts BTC to STO, and STC (the only other valid closing action) to BTO
        new_leg['action'] = config.ACTION_SELL_TO_OPEN if closing_leg['action'] == config.ACTION_BUY_TO_CLOSE else config.ACTION_BUY_TO_OPEN
        new_leg['quantity'] = closing_leg['quantity']
        new_opening_legs.append(new_leg)
              
      # 3. Combine the lists (2 closing + 2 opening)
      self.repeatingpanel_manual_legs.items = current_legs + new_opening_legs
    else:
      self.label_manual_entry_card.text = "Manual Entry: Close (record) existing position" 
      self.manual_entry_state = config.MANUAL_ENTRY_STATE_CLOSE
      self.dropdown_manual_existing_trade_change()

  def button_manual_entry_prefill_click(self, **event_args):
    """
    Reads the data from the Trade Ticket card and pre-fills the Manual Entry card.
    """
    if not self.trade_dto_list:
      print("no trade data to load into manual entry card")
      return

    # flatten the nested dto into legs
    leg_definitions = client_helpers._flatten_trade_dto(self, 
                                                        self.trade_dto_list, 
                                                        self.textbox_trade_entry_quantity.text
                                                       )
    print(f"leg defs are: {leg_definitions}")
    
    # Use the value that was actually submitted/previeweds to 
    # TODO:  switch this to getting underlying from the trade dto
    underlying = self.textbox_symbol.text 

    # 3. Populate the Manual Entry Card
    self.reset_card_manual_trade()
    # determin manual entry state of open or close or roll
    self.manual_entry_state = self.trade_ticket_state

    # 3. Handle Existing Trade (Close/Roll) vs New (Open)
    credit_debit_sign = 1
    if self.manual_entry_state in [config.MANUAL_ENTRY_STATE_ROLL, config.MANUAL_ENTRY_STATE_CLOSE]:
      if self.active_trade_row:
        # Fetch list so the dropdown works visually
        self.dropdown_manual_existing_trade.items = anvil.server.call('get_open_trades_for_dropdown', self.environment)
        self.dropdown_manual_existing_trade.selected_value = self.active_trade_row
        self.dropdown_manual_existing_trade.visible = True

        if self.manual_entry_state == config.MANUAL_ENTRY_STATE_ROLL:
          self.checkbox_manual_entry_roll.visible = True
          self.checkbox_manual_entry_roll.checked = True
        else:  #its a close whichis always a debit, so negative when saved in my DB.
          credit_debit_sign = -1
    else:
      # Open Logic
      self.dropdown_manual_transaction_type.selected_value = self.dropdown_strategy_picker.selected_value
      self.dropdown_manual_transaction_type.visible = True

    # 4. Fill Common Fields
    self.repeatingpanel_manual_legs.items = leg_definitions
    self.textbox_manual_credit_debit.text = self.textbox_overide_price.text if self.textbox_overide_price.text else self.textbox_net_credit.text
    try:
      self.textbox_manual_credit_debit.text = float(self.textbox_manual_credit_debit.text) * credit_debit_sign
    except (TypeError, ValueError):
      self.textbox_manual_credit_debit.text = None
    self.textbox_manual_underlying.text = underlying
    self.textbox_manual_underlying.visible = True

    # 5. Show Card
    self.button_save_manual_trade.enabled = True
    self.card_manual_entry.visible = True
          
  def button_manual_delete_trade_click(self, **event_args):
    """This method is called when the Delete Trade button is clicked"""
    # 1. Get the current trade from the dropdown
    trade_to_delete = self.dropdown_manual_existing_trade.selected_value
  
    if not trade_to_delete:
      return
  
      # 2. Confirm with the user
    if confirm(f"Are you sure you want to PERMANENTLY DELETE the trade for {trade_to_delete['Underlying']} and all its history?"):
      try:
        # 3. Call the server
        result = anvil.server.call('delete_trade', trade_to_delete)
        alert(result)
  
        # 4. Cleanup UI
        self.reset_card_manual_trade()
        self.card_manual_entry.visible = False
        self.refresh_open_positions_grid(refresh_risk=False)
  
      except Exception as e:
        alert(f"Error deleting trade: {e}")

  def load_trade_history(self):
    data = anvil.server.call('get_closed_trades', self.environment)
    self.repeatingpanel_trade_history.items = data['trades']
    self.label_agg_pl.text = f"Total P/L: ${data['total_pl']:.2f}"
    self.label_trade_rroc_ave.text = f"Trade RROC Ave: {data['trade_rroc_avg']:.2%}"
    self.label_portfolio_rroc_cum.text = f"Portfolio RROC Cum: {data['portfolio_rroc_cum']:.2%}"
    
    

  