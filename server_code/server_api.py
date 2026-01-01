import anvil.server
import datetime as dt
import random
from shared import config

# --- MOCK IMPLEMENTATION ---
# Switch this to False when ready to connect real Tradier code
IS_MOCK_MODE = True 

def get_environment_status() -> dict:
  """Returns environment data required by the orchestrator"""
  now = dt.datetime.now()
  # Mocking a valid trading time (e.g., 10:00 AM)
  # In production, this would be actual server time
  mock_time = dt.datetime.combine(now.date(), dt.time(10, 0))

  return {
    'status': 'OPEN', 
    'status_message': 'Market is open (Mock)',
    'today': now.date(),
    'now': mock_time,
    'is_holiday': False
  }

def get_current_positions() -> list[dict]:
  """
  Returns list of positions.
  """
  # Return empty list to simulate "No positions open" -> Triggers entry logic
  return []

def get_market_data_snapshot(cycle) -> dict:
  """
  Returns a dictionary of current market data for the cycle's instruments.
  Used by determine_cycle_state.
  """
  # Mock SPX moving normally
  return {
    'price': 5000.0,
    'open': 5000.0,
    'previous_close': 5000.0,
    # Add hedge quote if cycle has a hedge
    'hedge_last': 100.0
  }

def get_option_chain(date: dt.date) -> list[dict]:
  """
  Returns a mock option chain with enough data to pass evaluate_entry.
  """
  print(f"API: Fetching mock chain for {date}")

  # Generate a valid chain around 5000 SPX
  # We need a 20 delta put (approx 4925 strike)
  # We need a protection put (approx 4900 strike)

  chain = []

  # 1. Create the Short Target (20 Delta)
  chain.append({
    'symbol': 'SPXW_MOCK_SHORT',
    'strike': 4925,
    'option_type': 'put',
    'delta': -0.20,
    'bid': 6.0,
    'ask': 6.2, # Mid 6.10
    'expiration_date': date
  })

  # 2. Create the Long Target (Width 25 -> 4900)
  chain.append({
    'symbol': 'SPXW_MOCK_LONG',
    'strike': 4900,
    'option_type': 'put',
    'delta': -0.15,
    'bid': 5.0,
    'ask': 5.2, # Mid 5.10
    'expiration_date': date
  })

  # Net Credit = 6.10 - 5.10 = 1.00 (Valid!)

  # 3. Add some noise
  chain.append({'symbol': 'JUNK', 'strike': 4000, 'delta': -0.01, 'bid': 0.1, 'ask': 0.2, 'option_type': 'put', 'expiration_date': date})

  return chain

def open_spread_position(trade_data: dict) -> dict:
  """
  Simulates sending a multileg order to the broker.
  Returns the 'Execution Report' needed by server_db.
  """
  print(f"API: Opening Spread. Qty: {trade_data['quantity']}")

  # Simulate instant fill
  return {
    'id': f"ORD_MOCK_{random.randint(1000,9999)}",
    'price': trade_data['net_credit'], # We got the credit we asked for
    'time': dt.datetime.now(),
    'status': 'filled'
  }

# --- STUBS FOR OTHER ACTIONS (Not needed for Entry Test) ---

def close_all_positions(cycle):
  print(f"API: Closing all positions for Cycle {cycle.id}")

def execute_roll(trade, new_legs):
  print(f"API: Rolling Trade {trade.id}")

def close_position(trade):
  print(f"API: Closing Trade {trade.id}")

def buy_option(leg):
  print(f"API: Buying leg {leg}")