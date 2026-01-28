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
      self.label_roi_day.text = f"Daily ROI: {headlines['roi_day_pct']}%"
      self.label_cagr.text = f"Proj CAGR: {headlines['projected_cagr']}%"

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

  def _render_chart(self, data: dict) -> None:
    if not data or not data.get('dates'):
      self.plot_equity_curve.visible = False
      return

    self.plot_equity_curve.visible = True

    # 1. Define the Capital Trace (Left Axis - 'y1')
    trace_capital = go.Bar(
      x=data['dates'],
      y=data['capital'],
      name="Capital Risked",
      marker=dict(color='rgba(200, 200, 200, 0.4)'),
      yaxis='y' # Links to the primary y-axis
    )

    # 2. Define the PnL Trace (Right Axis - 'y2')
    trace_pnl = go.Scatter(
      x=data['dates'],
      y=data['cum_pnl'],
      name="Cumulative PnL",
      mode='lines+markers',
      line=dict(color='#2ecc71', width=3),
      yaxis='y2' # Links to the secondary y-axis
    )

    # 3. Assemble Figure with Manual Layout
    fig = go.Figure(data=[trace_capital, trace_pnl])

    fig.update_layout(
      title="Capital Efficiency & Account Growth",
      template="plotly_white",
      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
      margin=dict(l=50, r=50, t=80, b=40),

      # Primary Y-Axis (Left)
      yaxis=dict(
        title="Capital Risked ($)",
        titlefont=dict(color="gray"),
        tickfont=dict(color="gray")
      ),

      # Secondary Y-Axis (Right)
      yaxis2=dict(
        title="Net Realized Profit ($)",
        titlefont=dict(color="#2ecc71"),
        tickfont=dict(color="#2ecc71"),
        anchor="x",
        overlaying="y", # This makes it a dual-axis chart
        side="right"
      )
    )

    self.plot_equity_curve.figure = fig