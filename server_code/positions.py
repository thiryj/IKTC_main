import anvil.email
import anvil.secrets
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
import anvil.server
from typing import Dict

from tradier_python.models import Quote
import datetime
from types import SimpleNamespace

class DiagonalPutSpread:    
  def __init__(self, short_put: Quote, long_put: Quote):
    """
        Initializes a DiagonalPutSpread position from two option leg objects OR dictionaries (and converts them).
    """
    # Helper to convert dict to Quote if necessary
    def ensure_quote(item):
      # If it's already an object, just return it
      if not isinstance(item, dict):
        return item

      # --- PATCHING LOGIC START ---
      # Tradier Sandbox often sends incomplete dicts. 
      # We fill gaps so Quote(**item) validation passes.
      defaults = {
        'open_interest': 0,
        'volume': 0, 
        'last_volume': 0,
        'average_volume': 0,
        'greeks': None,
        'bid': 0.0,
        'ask': 0.0,
        'last': 0.0,
        'contract_size': 100,
        'bidexch': 'N/A', 
        'askexch': 'N/A',
        'description': 'No Description',
        'exch': 'N/A'
      }
      for k, v in defaults.items():
        if item.get(k) is None:
          item[k] = v

      # Try to create the real Quote object
      try:
        return Quote(**item)
      except Exception as e:
        print(f"Warning: Quote validation failed: {e}. Using Fallback Object.")

        # --- FALLBACK LOGIC ---
        # If strict validation still fails, create a "Mock Object" 
        # so the rest of the class doesn't crash on .attribute access.

        # 1. Handle Date Parsing (Crucial for DTE calc)
        if isinstance(item.get('expiration_date'), str):
          try:
            # Parse 'YYYY-MM-DD' to date object
            y, m, d = map(int, item['expiration_date'].split('-'))
            item['expiration_date'] = datetime.date(y, m, d)
          except:
            pass # Keep as string if parsing fails

        # 2. Handle Enum (get_dto calls .option_type.name)
        if isinstance(item.get('option_type'), str):
          # Create a tiny object so .name returns the string
          item['option_type'] = SimpleNamespace(name=item['option_type'])

        return SimpleNamespace(**item)
      # --- END ensure_quote ---
      
    # Data (Attributes)
    self.short_put = ensure_quote(short_put)
    self.long_put = ensure_quote(long_put)

    # Behavior (Methods) that calculate properties from the data
    self.net_premium = self.calculate_net_premium()
    self.margin = self.calculate_margin()
    self.ROM = self.net_premium / self.margin if (self.margin and self.margin > 0) else 0.0
    self.short_put_DTE = max(1, (self.short_put.expiration_date - datetime.date.today()).days)
    self.ROM_rate = self.ROM / self.short_put_DTE

  def calculate_net_premium(self):
    # Logic to calculate the total credit received
    premium_value = self.short_put.bid - self.long_put.ask
    return premium_value

  def calculate_cost_to_close(self):
    return self.short_put.ask - self.long_put.bid

  def calculate_margin(self):
    # Logic to calculate the position's margin requirement
    width = self.short_put.strike - self.long_put.strike
    margin = self.short_put.contract_size * (width - self.net_premium)
    return margin

  def describe(self):
    """A method to print a nice summary of the position."""
    print(f"Premium: ${self.net_premium:.2f}, ROM/day: {self.ROM_rate*self.short_put.contract_size:.2%}")

  def print_leg_details(self, leg_direction: str=None):  #leg_direction should be "short" or "long"
    """Prints leg details."""
    if leg_direction is None:
      leg_list=[self.short_put, self.long_put]
    elif leg_direction == "short":
      leg_list=[self.short_put]
    elif leg_direction == "long":
      leg_list=[self.long_put]
    else:
      print("must pass in a leg selection or None")
      return 
      
    for leg in leg_list:
      print(
        f"Symbol: {leg.symbol}, "
        f"Type: {leg.option_type}, "
        f"Strike: {leg.strike}, "
        f"Expiry: {leg.expiration_date}, "
        f"Last: {leg.last}"
      )

  def get_dto(self)->Dict:
    position_dto = {
      # --- Top-level calculated metrics ---
      'net_premium': self.net_premium,
      'margin': self.margin,
      'ROM': self.ROM,
      'short_put_DTE': self.short_put_DTE,
      'ROM_rate': self.ROM_rate,

      # --- Nested dictionary for the short put leg ---
      # We assume the 'Quote' object has these attributes based on your methods
      'short_put': {
        'symbol': self.short_put.symbol,
        'option_type': self.short_put.option_type.name,
        'strike': self.short_put.strike,
        'expiration_date': self.short_put.expiration_date,
        'bid': self.short_put.bid,
        'ask': self.short_put.ask,
        'last': self.short_put.last,
        'contract_size': self.short_put.contract_size
      },

      # --- Nested dictionary for the long put leg ---
      'long_put': {
        'symbol': self.long_put.symbol,
        'option_type': self.long_put.option_type.name,
        'strike': self.long_put.strike,
        'expiration_date': self.long_put.expiration_date,
        'bid': self.long_put.bid,
        'ask': self.long_put.ask,
        'last': self.long_put.last,
        'contract_size': self.long_put.contract_size
      }
    }
    return position_dto
      