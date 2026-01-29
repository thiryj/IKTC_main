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
    self.last_panic_date = row['last_panic_date']
    self.notes = row['notes']

    # Link Wrappers (Data Navigation Only)
    self.rule_set = RuleSet(row['rule_set']) if row['rule_set'] else None
    self.hedge_trade = Trade(row['hedge_trade']) if row['hedge_trade'] else None
    self.trades = []
    self.hedge_trade_link = None 

    self._effective_rules = self._calculate_effective_rules(row['rule_set'])

  def _calculate_effective_rules(self, rules_row):
    if not rules_row: return {}

    r = dict(rules_row) # Create the mutable dict once

    # Only perform scaling if we are in the Sandbox/SPY environment
    if self.underlying == config.TARGET_UNDERLYING[config.ENV_SANDBOX]:
      scale_keys = ['spread_width', 'spread_min_premium', 'spread_max_premium', 
                    'roll_max_debit', 'panic_threshold_dpu', 'spread_size_factor']
      for k in scale_keys:
        if r.get(k) is not None:
          if k == 'spread_width':
            r[k] = round(r[k] / 10.0)
          else:
            r[k] = r[k] / 10.0
      r['spread_min_premium'] = 0.0 # Standard sandbox behavior

    return r

  @property
  def rules(self) -> dict:
    return self._effective_rules

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
    self.notes = row['notes']

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