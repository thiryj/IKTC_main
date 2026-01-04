from typing import TypedDict, Dict, Optional

# 1. Sub-components (The details)
class Greeks(TypedDict):
  delta: float
  theta: float
  vega: float
  iv: float

class Quote(TypedDict):
  price: float        # Mid or Mark price
  ask: float
  bid: float
  volume: int
  open_interest: int

# 2. The Data Contract (The main object)
class InstrumentData(TypedDict):
  symbol: str
  quote: Quote
  greeks: Optional[Greeks] # Optional because stocks/indices don't have greeks, only options do
  dte: Optional[int]       # None for underlying

class MarketData(TypedDict):
  price: float
  open: float
  previous_close: float
  hedge_last: float
  spread_marks: Dict[str, float] # Maps Trade ID -> Cost to Close

'''{
  # Underlying (SPX/SPY) Data
  'price': 5000.0,           # Float: Current Last Price
  'open': 4950.0,            # Float: Today's Open (Cleaned for Sandbox 0s)
  'previous_close': 4900.0,  # Float: Yesterday's Close

  # Hedge Data
  'hedge_last': 55.0,        # Float: Current Price of the Hedge Option (or 0.0 if missing)

  # Active Spread Pricing (The "Marks")
  'spread_marks': {
      'row_id_123': 0.15,    # Key=Trade ID (str), Value=Net Debit to Close (float)
      'row_id_456': 0.40
  }
}
'''