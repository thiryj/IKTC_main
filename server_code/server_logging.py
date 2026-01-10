import anvil.server
import anvil.tables as tables
from anvil.tables import app_tables
import datetime as dt
import json, pytz

from shared import config

# Universal Logger Function
@anvil.server.callable
def log(message: str, level: int = config.LOG_INFO, source: str = "System", context: dict = None):
  """
    Centralized Logging Handler.
    Routes messages to Console, DB, and Alerts based on Config Thresholds.
    
    Args:
        message: The text content.
        level: config.LOG_DEBUG, .LOG_INFO, etc.
        source: Where did this come from? (e.g. "EntryLogic", "BrokerAPI")
        context: Optional dict of IDs or data (e.g. {'trade_id': '123'})
    """
  # Only apply time limits to low-priority logs (INFO/DEBUG).
  # Always let WARNING/CRITICAL through.
  if level < config.LOG_WARNING:
    # Get Current Eastern Time
    utc_now = dt.datetime.now(pytz.utc)
    eastern = pytz.timezone('US/Eastern')
    now_et = utc_now.astimezone(eastern).time()
    if now_et < config.LOG_START_TIME or now_et > config.LOG_STOP_TIME:
      return # Silent exit

  # 1. CONSOLE (Immediate Print)
  if level >= config.LEVEL_CONSOLE:
    lvl_name = config.LOG_NAMES.get(level, "UNKNOWN")
    # Format: [INFO] [System] Message...
    print(f"[{lvl_name}] [{source}] {message}")

    # 2. DATABASE (Persistent Record)
  if level >= config.LEVEL_DB:
    try:
      # Convert context to string for storage if present
      data_str = json.dumps(context, default=str) if context else None

      app_tables.logs.add_row(
        timestamp=dt.datetime.now(),
        level=config.LOG_NAMES.get(level, "UNKNOWN"),
        source=source,
        message=message,
        data=data_str,
        environment=config.ACTIVE_ENV
      )
    except Exception as e:
      # Fallback if DB fails (don't crash the bot)
      print(f"!! LOGGING ERROR !! Failed to write to DB: {e}")

    # 3. ALERTS (Async / Human)
  if level >= config.LEVEL_ALERT:
    # Launch background task so we don't block the trading logic
    # while waiting for Email/SMS servers.
    anvil.server.launch_background_task('send_alert_async', message, level, source)

# --- BACKGROUND TASKS ---

@anvil.server.background_task
def send_alert_async(message, level, source):
  """Handles slow notifications (Email/SMS)"""
  lvl_name = config.LOG_NAMES.get(level, "CRITICAL")
  subject = f"[{config.ACTIVE_ENV}] {lvl_name}: {source}"

  # Example: Send Email (Anvil Native)
  # anvil.email.send(
  #     to="your-email@example.com",
  #     subject=subject,
  #     text=message
  # )

  # Placeholder print to prove async works
  print(f">> ASYNC ALERT SENT: {subject} - {message}")
