import datetime as dt

# Environment section: PROD or SANDBOX
ENV_SANDBOX = 'SANDBOX'
ENV_PROD = 'PROD'
IS_PROD = True  # Master - bot level environment selector.  True = PROD, False = SANDBOX
#DRY_RUN = False  # Moved to settings
IGNORE_SCHEDULUED_TASKS = False

ACTIVE_ENV = ENV_PROD if IS_PROD else ENV_SANDBOX
TARGET_UNDERLYING = {
  ENV_PROD: 'SPX',
  ENV_SANDBOX: 'SPY'
}

# Guardrail flags
ENFORCE_TRADING_HOURS = True   #disable to allow after hours automation for testing - refers to market open/close
ENFORCE_LATE_OPEN_GUARDRAIL = True
ENFORCE_ZOMBIE_CHECKS = False
ENFORCE_CONSISTENCY_CHECKS = False
ENFORCE_FREQUENCY_CHECKS = True
HARVEST_NAKED_HEDGE = True

# Risk Management
# Hard cap on leverage. Even if spreads are $0.05, we limit the ratio.
# Example: 1 Hedge -> Max 6 Spreads. 5 Hedges -> Max 30 Spreads.
MAX_SPREAD_TO_HEDGE_RATIO = 6
ROLL_TRIGGER_CEILING = 0.5  #applies to rolled spreads - can't wait until 3x entry price, too high
KELLY_QTR = 0.117
QTY_OVERIDE = 1


# Order Execution Limits
ORDER_TIMEOUT_SECONDS = 15

# Cycle Status
STATUS_NEW = 'NEW'
STATUS_OPEN = 'OPEN'
STATUS_CLOSED = 'CLOSED'

# Trade Roles
ROLE_HEDGE = 'HEDGE'
ROLE_INCOME = 'INCOME' # The daily spread

# Income spread entry reasons
REASON_FRESH = "FRESH"
REASON_ROLL = "ROLL"  # rolls that were filled on the first try
REASON_RECOVERY = "RECOVERY"  # subset of defensive rolls that needed additional hunting to find

# Leg side: long/short
LEG_SIDE_SHORT = "short"
LEG_SIDE_LONG = "long"

# Automation States (The decisions made by server_libs)
#STATE_HARVEST_TARGET_HIT = 'HARVEST_TARGET_HIT'
#STATE_SPREAD_MISSING = 'SPREAD_MISSING'
STATE_IDLE = 'IDLE'
STATE_WAITING = 'WAITING'
STATE_ENTRY_WINDOW = 'ENTRY_WINDOW'
STATE_ACTIVE_HUNT = 'ACTIVE_HUNT' # After entry, waiting for $3.50 touch
STATE_EOD_CLEANUP = 'EOD_CLEANUP'

# Alert Levels
ALERT_CRITICAL = 'CRITICAL'
ALERT_INFO = 'INFO'

# Tradier API enums
TRADIER_OPTION_TYPE_PUT = 'put'
TRADIER_OPTION_TYPE_CALL = 'call'

# Misc statics
STRATEGY_SCALPEL = 'OTM_GAMMA_SCALPEL'
TARGET_DEBIT_MIN = 1.20
TARGET_DEBIT_MAX = 1.35
HARVEST_TARGET_PX = 3.50
MARKET_OPEN_TIME = dt.time(9, 30)
DEFAULT_MULTIPLIER = 100
MAX_DELTA_ERROR = 0.05   # short strike of income spread must be target_detla +/- MAX_DELTA_ERROR
MAX_BID_ASK_SPREAD = .75
UI_REFRESH_SECONDS = 60

ACTIVE_RULESET = 'rule_set_1'

# Levels (Standard Python integer mapping)
LOG_DEBUG = 10
LOG_INFO = 20
LOG_WARNING = 30
LOG_CRITICAL = 40

# Level Names (for display)
LOG_NAMES = {
  10: "DEBUG",
  20: "INFO",
  30: "WARNING",
  40: "CRITICAL"
}

LOG_SOURCE_ORCHESTRATOR = 'main'
LOG_SOURCE_API = 'api'
LOG_SOURCE_DB = 'db'
LOG_SOURCE_LIBS = 'libs'
LOG_SOURCE_SERVER_CLIENT = 'server_client'
LOG_SOURCE_CLIENT = 'client'
LOG_SOURCE_LOGGER = 'logging'

# Thresholds (The "Waterline" for each channel)
# Change these to tune the noise level without touching code
LEVEL_CONSOLE = LOG_DEBUG    # Print everything
LEVEL_DB = LOG_INFO          # Record events, warnings, errors (Skip debug noise)
LEVEL_ALERT = LOG_CRITICAL   # Only wake human for disasters
LOG_START_TIME = dt.time(9, 0)  # 9:00 AM ET
LOG_STOP_TIME = dt.time(17, 0)  # 5:00 PM ET

MARKET_HOLIDAYS = [
  dt.date(2026, 1, 1),   # New Year's
  dt.date(2026, 1, 19),  # MLK Day
  dt.date(2026, 2, 16),  # Presidents' Day
  dt.date(2026, 4, 3),   # Good Friday
  dt.date(2026, 5, 25),  # Memorial Day
  dt.date(2026, 6, 19),  # Juneteenth
  dt.date(2026, 7, 3),   # Independence Day (Observed)
  dt.date(2026, 9, 7),   # Labor Day
  dt.date(2026, 11, 26), # Thanksgiving
  dt.date(2026, 12, 25), # Christmas
]

# log sample
# logger.log(f"", level=, source=, context={})
