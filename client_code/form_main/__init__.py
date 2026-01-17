from ._anvil_designer import form_mainTemplate
from anvil import *
import plotly.graph_objects as go
import anvil.server
import datetime
from ..shared import config

class form_main(form_mainTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.REFRESH_RATE_UI = config.UI_REFRESH_SECONDS

    self.check_box_automation.set_event_handler('change', self.check_box_automation_change)
    self.button_refresh_logs.set_event_handler('click', self.button_refresh_logs_click)
    self.button_refresh_ui.set_event_handler('click', self.button_refresh_ui_click)
    
    # 1. Initial Load
    self.refresh_ui()
    self.refresh_logs()

    # Initialize timer state
    self.timer_refresh.interval = self.REFRESH_RATE_UI if self.check_box_automation.checked else 0

  def refresh_ui(self, **event_args):
    """
    Called by Timer Tick and Init. 
    Fetches the state dict from server_client.
    """
    try:
      # Call the read-only view function
      state = anvil.server.call('get_dashboard_state')
      if state.get('bot_is_stale') and state['automation_enabled']:
        self.label_decision_state.text = "BOT OFFLINE (STALE)"
        self.label_decision_state.foreground = "orange"
      else:
        self.label_decision_state.text = state['bot_status_text']
        self.label_decision_state.foreground = state['bot_status_color']

      # --- Global Status ---
      self.label_active_env.text = f"Env: {state['active_env']}"
      self.label_active_env.border = "1px solid green"
      
      self.check_box_automation.checked = state['automation_enabled']
      
      self.label_market_status.text = f"Market: {state.get('market_status', 'UNKNOWN')}"
      if state['market_status'] == 'OPEN':
        #self.label_market_status.foreground = "green"
        self.label_market_status.border = "1px solid green"
      else:
        #self.label_market_status.foreground = "gray"
        self.label_market_status.border = "1px solid grey"
        
      self.label_decision_state.border = "1px solid black"
      self.label_decision_state.text = state['bot_status_text']
      self.label_decision_state.foreground = state['bot_status_color']

      # PnL
      pnl = state['net_daily_pnl']
      if pnl is None:
        pnl = 0.0
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
      self._render_spread_gauge(state.get('spread_gauge'))

      # --- Closed Spread Card ---
      closed = state['closed_session']
      self.card_closed_spread.visible = closed['visible']
      if closed['visible']:
        # Populate Data
        self.label_closed_desc.text = closed['text']
        self.label_closed_pnl.text = f"${closed['pnl']:,.2f}"
        self.label_closed_pnl.foreground = closed['color']

    except Exception as e:
      print(f"UI Refresh Error: {e}")
      self.label_decision_state.text = "CONNECTION LOST"
      self.label_decision_state.foreground = "red"
      #Notification("Lost connection to server", style="warning", timeout=2).show()

  def refresh_logs(self):
    """Refreshes the log grid."""
    # This returns a SearchIterator, so it's lazy and efficient
    self.repeating_panel_logs.items = anvil.server.call('get_log_stream')

  # --- Event Handlers ---

  def check_box_automation_change(self, **event_args):
    """Toggle the master switch and UI timer."""
    automation_enabled = self.check_box_automation.checked
    anvil.server.call('toggle_automation_status', automation_enabled)
    #Notification(f"Automation {'ENABLED' if automation_enabled else 'DISABLED'}").show()
    self.refresh_ui()
    if automation_enabled:
      self.timer_refresh.interval = self.REFRESH_RATE_UI # Turn ON
      self.button_refresh_ui.border = '1px solid green'
    else:
      self.timer_refresh.interval = 0 # Turn OFF
      self.button_refresh_ui.border = None

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

  @handle("timer_refresh", "tick")
  def timer_refresh_tick(self, **event_args):
    """Auto-refresh"""
    self.refresh_ui()
    self.refresh_logs()

  def button_refresh_logs_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.refresh_logs()

  def button_refresh_ui_click(self, **event_args):
    """This method is called when the button is clicked"""
    self.refresh_ui()

  def _render_spread_gauge(self, gauge_data):
    """
    Renders the Bullet Chart for the Spread.
    Visualizes: Target (Green) -> Entry (Gray) -> Trigger (Red).
    """
    self.plot_spread_gauge.visible = bool(gauge_data)

    if not gauge_data: return

    try:
      current = float(gauge_data.get('current', 0) or 0)
      entry = float(gauge_data.get('entry', 0) or 0)
      target = float(gauge_data.get('target', 0) or 0)
      trigger = float(gauge_data.get('trigger', 0) or 0)
    except (TypeError, ValueError):
      self.plot_spread_gauge.visible = False
      return


    # Define Scale Max (give some headroom above trigger)
    max_val = max(trigger * 1.15, current * 1.1, 1.0)

    fig = go.Figure(go.Indicator(
      mode = "number+gauge+delta",
      value = current,
      # Delta shows change from Entry Price (Profit/Loss indicator)
      delta = {
        'reference': entry, 
        'increasing': {'color': "red"}, # Cost going UP is BAD
        'decreasing': {'color': "green"} # Cost going DOWN is GOOD
      },
      number = {'prefix': "$", 'font': {'size': 20}},
      title = {'text': "Cost to Close", 'font': {'size': 14}},
      domain = {'x': [0, 1], 'y': [0, 1]},
      gauge = {
        'shape': "bullet",
        'axis': {'range': [0, max_val]}, 
        'threshold': {
          'line': {'color': "red", 'width': 2},
          'thickness': 0.75,
          'value': trigger
        },
        'steps': [
          {'range': [0, target], 'color': "rgba(0, 200, 0, 0.3)"},   # Profit Zone (Green)
          {'range': [target, entry], 'color': "rgba(200, 200, 200, 0.3)"}, # Giving back Profit (Gray)
          {'range': [entry, max_val], 'color': "rgba(200, 0, 0, 0.3)"}     # Loss Zone (Red)
        ],
        'bar': {'color': "black"} # The Current Price Marker
      }
    ))

    # Tight Layout
    fig.update_layout(margin={'t':0, 'b':0, 'l':20, 'r':20}, height=50)
    self.plot_spread_gauge.figure = fig
