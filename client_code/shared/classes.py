import anvil.server

from . import config

@anvil.server.portable_class
class Cycle:
  def __init__(self, row=None):
    if row:
      self.id = row.get_id()
      self.account = row['Account']
      self.underlying = row['Underlying']
      self.status = row['Status']
      self.net_pl = row['NetPL'] or 0.0
      self.daily_hedge_ref = row['DailyHedgeRef'] or 0.0
      self.hedge_trade_link = row['HedgeTrade']
      self.trades = [] 
      rules_row = row['RuleSet']
      if not rules_row:
        raise ValueError(f"Cycle {self.id} is missing a RuleSet")

      # Validation Helper: Ensure key exists and is not None
      def get_rule(col_name):
        val = rules_row[col_name]
        if val is None:
          raise ValueError(f"RuleSet {rules_row.get_id()} is missing value for '{col_name}'.")
        return val

        # Map DB Columns (CamelCase) -> Python Dict (snake_case)
        self.rules = {
          'hedge_delta':          get_rule('HedgeDelta'),
          'target_hedge_dte':     get_rule('HedgeDTE'),
          'spread_delta':         get_rule('SpreadDelta'),
          'harvest_fraction':     get_rule('HarvestFraction'),
          'roll_trigger_mult':    get_rule('RollTriggerMultiple'),
          'panic_threshold_dpu':  get_rule('PanicHarvestDPU'),
          'spread_size_magic':    get_rule('SpreadSizingMagicNum')
        }
    else:
      self.rules = {}

@anvil.server.portable_class
class Trade:
  def __init__(self, row=None):
    if row:
      self.id = row.get_id()
      self.role = row['Role'] 
      self.status = row['Status']
      self.entry_credit = row['EntryCredit'] or 0.0
      self.roll_trigger = row['RollTriggerPrice']
      self.capital_req = row['CapitalRequired'] or 0.0
      self.total_pl = row['TotalPL'] or 0.0
      self.cycle_link = row['Cycle']
      self.legs = []
    else:
      self.id, self.role = None, None
      self.status = config.STATUS_OPEN
      self.entry_credit, self.roll_trigger, self.capital_req = 0.0, None, 0.0
      self.total_pl, self.cycle_link, self.legs = 0.0, None, []

@anvil.server.portable_class
class Leg:
  def __init__(self, row=None):
    if row:
      self.id = row.get_id()
      self.action = row['Action']
      self.quantity = row['Quantity']
      self.expiration = row['Expiration']
      self.strike = row['Strike']
      self.option_type = row['OptionType']
      self.symbol = row['OCCSymbol']
      self.trade_link = row['Trade']
      self.txn_link = row['Transaction']
    else:
      self.id, self.action, self.quantity = None, None, 0
      self.expiration, self.strike, self.option_type = None, 0.0, None
      self.symbol, self.trade_link, self.txn_link = None, None, None

@anvil.server.portable_class
class Transaction:
  def __init__(self, row=None):
    if row:
      self.id = row.get_id()
      self.date = row['Date']
      self.type = row['Type']
      self.amount = row['CreditDebit'] or 0.0
      self.order_id = row['TradierOrderID']
      self.trade_link = row['Trade']
    else:
      self.id, self.date, self.type = None, None, None
      self.amount, self.order_id, self.trade_link = 0.0, None, None