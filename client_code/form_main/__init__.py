from ._anvil_designer import form_mainTemplate
from anvil import *
import anvil.server
import datetime

class form_main(form_mainTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)

    self.check_box_automation.set_event_handler('change', self.check_box_automation_change)
    self.button_refresh_logs.set_event_handler('click', self.button_refresh_logs_click)
    self.button_refresh_ui.set_event_handler('click', self.button_refresh_ui_click)
    
    # 1. Initial Load
    self.refresh_ui()
    self.refresh_logs()

  def refresh_ui(self, **event_args):
    """
    Called by Timer Tick and Init. 
    Fetches the state dict from server_client.
    """
    try:
      # Call the read-only view function
      state = anvil.server.call('get_dashboard_state')

      # --- Global Status ---
      self.check_box_automation.checked = state['automation_enabled']
      self.label_market_status.text = f"Market: {state['market_status']}"

      # Color code market status
      if state['market_status'] == 'OPEN':
        self.label_market_status.foreground = "green"
      else:
        self.label_market_status.foreground = "gray"

      # PnL
      pnl = state['net_daily_pnl']
      self.label_pnl_day.text = f"Day PnL: ${pnl:,.2f}"
      self.label_pnl_day.foreground = "green" if pnl >= 0 else "red"

      # --- Hedge Card ---
      h = state['hedge']
      self.label_hedge_status.text = h['symbol']
      self.label_hedge_status.foreground = h['status_color']
      self.label_hedge_details.text = h['details']

      # --- Spread Card ---
      s = state['spread']
      self.label_spread_status.text = s['symbol']
      self.label_spread_status.foreground = s['status_color']
      self.label_spread_details.text = s['details']

    except Exception as e:
      print(f"UI Refresh Error: {e}")
      Notification("Lost connection to server", style="warning").show()

  def refresh_logs(self):
    """Refreshes the log grid."""
    # This returns a SearchIterator, so it's lazy and efficient
    self.repeating_panel_logs.items = anvil.server.call('get_log_stream')

  # --- Event Handlers ---

  def check_box_automation_change(self, **event_args):
    """Toggle the master switch."""
    new_state = self.check_box_automation.checked
    anvil.server.call('toggle_automation_status', new_state)
    Notification(f"Automation {'ENABLED' if new_state else 'DISABLED'}").show()

  def button_panic_click(self, **event_args):
    """The Big Red Button."""
    c = confirm("Are you sure you want to LIQUIDATE ALL POSITIONS immediately?", 
                title="Confirm Emergency Exit", 
                buttons=[("LIQUIDATE", True, "danger"), ("Cancel", False)])

    if c:
      self.button_panic.enabled = False
      self.button_panic.text = "CLOSING..."
      try:
        # Call the manual trigger
        result_msg = anvil.server.call('manual_panic_trigger')
        alert(result_msg, title="Panic Sequence")
      except Exception as e:
        alert(f"Failed to trigger panic: {e}")
      finally:
        self.button_panic.enabled = True
        self.button_panic.text = "EMERGENCY CLOSE"
        self.refresh_ui()

  def timer_refresh_tick(self, **event_args):
    """Auto-refresh every 30s"""
    self.refresh_ui()
    # Optional: Refresh logs less frequently or on same tick
    # self.refresh_logs()

  def button_refresh_logs_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.refresh_logs()

  def button_refresh_ui_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.refresh_ui()
