from . import config

class RuleSet:
  def __init__(self, row):
    self._row = row
    self.name = row['name']
    self.description = row['description']

class Cycle:
  def __init__(self, row):
    self._row = row
    self.id = row.get_id()
    self.account = row['account']
    self.underlying = row['underlying']
    self.status = row['status']
    self.start_date = row['start_date']
    self.end_date = row['end_date']
    self.daily_hedge_ref = row['daily_hedge_ref']
    self.total_pnl = row['total_pnl']
    self.notes = row['notes']

    # Link Wrappers (Data Navigation Only)
    self.rule_set = RuleSet(row['rule_set']) if row['rule_set'] else None
    self.hedge_trade = Trade(row['hedge_trade']) if row['hedge_trade'] else None
    self.trades = []
    self.hedge_trade_link = None 

  @property
  def rules(self) -> dict:
    """
    Returns the Effective Rules as a dictionary.
    Automatically handles scaling for SPY (1/10th) vs SPX.
    """
    if not self.rule_set:
      return {}
  
    # Convert anvil Row to mutable dict
    r = dict(self.rule_set._row)
  
    # Sandbox/SPY Scaling Logic
    if self.underlying == config.TARGET_UNDERLYING[config.ENV_SANDBOX]:
  
      # List of keys that need to be divided by 10 for SPY
      scale_keys = [
        'spread_width', 
        'spread_min_premium', 
        'spread_max_premium', 
        'roll_max_debit', 
        'panic_threshold_dpu'
      ]
  
      for k in scale_keys:
        if r.get(k) is not None:
          val = r[k] / 10.0
          if k == 'spread_width':
            r[k] = round(val)
          else:
            r[k] = val
    return r

class Trade:
  def __init__(self, row):
    self._row = row
    self.id = row.get_id()
    self.role = row['role'] 
    self.status = row['status']
    self.quantity = row['quantity']
    self.entry_price = row['entry_price']
    self.exit_price = row['exit_price']
    self.capital_required = row['capital_required']
    self.target_harvest_price = row['target_harvest_price']
    self.roll_trigger_price = row['roll_trigger_price']
    self.pnl = row['pnl']
    self.entry_time = row['entry_time'] # Renamed
    self.exit_time = row['exit_time']   # Renamed
    self.order_id_external = row['order_id_external']

  @property
  def cycle(self):
    return Cycle(self._row['cycle']) if self._row['cycle'] else None

class Leg:
  def __init__(self, row):
    self._row = row
    self.id = row.get_id()
    self.side = row['side'] 
    self.quantity = row['quantity']
    self.occ_symbol = row['occ_symbol']
    self.strike = row['strike']
    self.option_type = row['option_type']
    self.expiry = row['expiry']
    self.active = row['active']
    self.id_external = row['id_external']

    # Store raw rows for links
    self.opening_transaction = row['opening_transaction']
    self.closing_transaction = row['closing_transaction']

  @property
  def trade(self):
    return Trade(self._row['trade']) if self._row['trade'] else None

class Transaction:
  def __init__(self, row):
    self._row = row
    self.id = row.get_id()
    self.action = row['action'] 
    self.price = row['price']
    self.quantity = row['quantity']
    self.fees = row['fees']
    self.timestamp = row['timestamp']
    self.order_id_external = row['order_id_external']

  @property
  def trade(self):
    return Trade(self._row['trade']) if self._row['trade'] else None