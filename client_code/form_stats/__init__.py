from ._anvil_designer import form_statsTemplate
import anvil.server
import plotly.graph_objects as go

class form_stats(form_statsTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.refresh_dashboard()

  def refresh_dashboard(self) -> None:
    """Fetches all 3 data buckets and populates the UI."""
    # 1. Headlines
    headlines = anvil.server.call('get_performance_headlines')
    if headlines.get('active'):
      self.label_total_pnl.text = f"Total Net: ${headlines['total_pnl']:,.2f}"
      self.label_roi_day.text = f"Daily ROI: {headlines['roi_day_pct']:.2f}%"
      self.label_cagr.text = f"Proj CAGR: {headlines['projected_cagr']:.2f}%"

      # 2. Efficiency / EV
    eff = anvil.server.call('get_strategic_efficiency')
    if eff and eff.get('trade_count', 0) > 0:
      self.label_ev_actual.text = f"Actual EV: ${eff['actual_ev']:.2f}"
      self.label_ev_target.text = f"Target EV: ${eff['theoretical_ev']:.2f}"

      # Alpha Formatting
      alpha = eff['alpha']
      color = "green" if alpha >= 0 else "red"
      self.label_alpha.text = f"Alpha: ${alpha:+.2f}/trade"
      self.label_alpha.foreground = color

      self.label_stop_cost.text = f"Avg Stop Cost: ${eff['roll_stop_avg_dollars']:.2f}"

      # 3. Chart
    chart_data = anvil.server.call('get_equity_curve_data')
    self._render_chart(chart_data)

    # 4. Live Continuous Pulse
    pulse = anvil.server.call('get_continuous_pulse_stats')
    if pulse.get('is_active'):
      # Net Liq is your Realized + Unrealized
      self.label_net_liq.text = f"Net Liquidation: ${pulse['net_liquidation_pnl']:,.2f}"
      self.label_net_liq.foreground = "green" if pulse['net_liquidation_pnl'] >= 0 else "red"

      # Show the "Drag" or "Boost" from open trades
      unrealized = pulse['unrealized_pnl']
      self.label_unrealized.text = f"Open Unrealized: ${unrealized:,.2f}"
      self.label_unrealized.foreground = "#5bc0de" # Blueish for 'floating' value

      self.label_realized_lifetime.text = f"Lifetime Realized: ${pulse['realized_pnl']:,.2f}"

    # 5. KPI Benchmarks
    kpi = anvil.server.call('get_kpi_benchmarks')
    if kpi:
      self._render_kpi_gauge(self.plot_win_rate, "Win Rate %", kpi['win_rate'], 45, 55, 65, 100)
      self._render_kpi_gauge(self.plot_profit_factor, "Profit Factor", kpi['profit_factor'], 1.3, 1.8, 2.2, 3.5)
      self._render_kpi_gauge(self.plot_avg_win, "Avg Winner $", kpi['avg_winner'], 200, 210, 225, 300)
      # Note: For consec losses, high numbers are BAD, so we flip the colors logic
      self._render_consec_gauge(self.plot_consec_losses, "Consec Losses", kpi['consec_losses'])

  def _render_chart(self, data: dict) -> None:
    if not data or not data.get('dates'):
      self.plot_equity_curve.visible = False
      return

    self.plot_equity_curve.visible = True

    # Dynamic mode: use markers only for 1 point, lines+markers for multiple
    chart_mode = 'lines+markers' if len(data['dates']) > 1 else 'markers'

    # 1. Define Capital Trace (Bars)
    trace_capital = go.Bar(
      x=data['dates'],
      y=data['capital'],
      name="Capital Risked",
      marker=dict(color='rgba(200, 200, 200, 0.4)'),
      yaxis='y'
    )

    # 2. Define PnL Trace (Line)
    trace_pnl = go.Scatter(
      x=data['dates'],
      y=data['cum_pnl'],
      name="Cumulative PnL",
      mode=chart_mode,
      line=dict(color='#2ecc71', width=3),
      yaxis='y2'
    )

    # 3. Assemble and Layout
    fig = go.Figure(data=[trace_capital, trace_pnl])

    fig.update_layout(
      title="Capital Efficiency & Account Growth",
      template="plotly_white",
      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
      margin=dict(l=50, r=50, t=80, b=40),

      yaxis=dict(
        title="Capital Risked ($)",
        titlefont=dict(color="gray"),
        tickfont=dict(color="gray")
      ),
      yaxis2=dict(
        title="Net Realized Profit ($)",
        titlefont=dict(color="#2ecc71"),
        tickfont=dict(color="#2ecc71"),
        anchor="x",
        overlaying="y",
        side="right"
      )
    )

    self.plot_equity_curve.figure = fig

  def _render_kpi_gauge(self, component, title, value, warn, low_target, high_target, max_val):
    """Renders a standard 'High is Good' bullet chart with fixed layout."""

    # Force value to 0 if None to prevent Plotly errors
    display_val = value if value is not None else 0

    fig = go.Figure(go.Indicator(
      mode = "number+gauge", 
      value = display_val,
      number = {
        'font': {'size': 24, 'color': '#333'}, # Smaller, cleaner number
        'suffix': "%" if "Rate" in title else "" # Auto-add % for Win Rate
      },
      title = {'text': title, 'font': {'size': 12}, 'align': 'left'},
      domain = {'x': [0.15, 1], 'y': [0, 1]}, # Gives the Title on the left more room
      gauge = {
        'shape': "bullet",
        'axis': {
          'range': [0, max_val],
          'tickmode': 'array',
          'tickvals': [warn, low_target, high_target], # Show lines at your key thresholds
          'tickwidth': 1,
          'tickcolor': "black"
        },
        'steps': [
          {'range': [0, warn], 'color': "rgba(255, 0, 0, 0.15)"},       # Red
          {'range': [warn, low_target], 'color': "rgba(255, 255, 0, 0.15)"}, # Yellow
          {'range': [low_target, high_target], 'color': "rgba(0, 255, 0, 0.15)"} # Green
        ],
        'bar': {'color': "#2c3e50", 'thickness': 0.4} # The actual performance bar
      }
    ))

    # Margin adjustment to prevent clipping
    fig.update_layout(
      margin={'t':25, 'b':25, 'l':10, 'r':40}, 
      height=80, 
      template="plotly_white"
    )
    component.figure = fig
  
  def _render_consec_gauge(self, component, title, value):
    """Renders a 'Low is Good' bullet chart for Streaks."""
    fig = go.Figure(go.Indicator(
      mode = "number+gauge", value = value,
      title = {'text': title, 'font': {'size': 14}},
      gauge = {
        'shape': "bullet",
        'axis': {'range': [0, 8]},
        'steps': [
          {'range': [0, 4], 'color': "rgba(0, 255, 0, 0.2)"},   # Safe (Green)
          {'range': [4, 6], 'color': "rgba(255, 255, 0, 0.2)"}, # Warning (Yellow)
          {'range': [6, 8], 'color': "rgba(255, 0, 0, 0.2)"}    # Danger (Red)
        ],
        'bar': {'color': "black"}
      }
    ))
    fig.update_layout(margin={'t':30, 'b':10, 'l':100, 'r':20}, height=60)
    component.figure = fig