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

# 3. The Lookup Dictionary (Key = OCC Symbol)
# This is what you pass into server_libs
MarketData = Dict[str, InstrumentData]