import anvil.email
import anvil.server
import anvil.tables as tables
from anvil.tables import app_tables
import anvil.tables.query as q
import datetime as dt
import json, pytz
import requests

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
  # 1. Weekend Check (5=Saturday, 6=Sunday)
  if level < config.LOG_WARNING and config.ENFORCE_TRADING_HOURS and not config.DRY_RUN:
    # Get Current Eastern Time
    utc_now = dt.datetime.now(pytz.utc)
    eastern = pytz.timezone('US/Eastern')
    now_full = utc_now.astimezone(eastern)
    
    today_date = now_full.date()
    now_time = now_full.time()
    
    if today_date.weekday() >= 5: return 
    if today_date in config.MARKET_HOLIDAYS: return
    if now_time < config.LOG_START_TIME or now_time > config.LOG_STOP_TIME: return # Silent exit
      

  # 1. CONSOLE (Immediate Print)
  if level >= config.LEVEL_CONSOLE:
    lvl_name = config.LOG_NAMES.get(level, "UNKNOWN")
    # Format: [INFO] [System] Message...
    print(f"[{lvl_name}] [{source}] {message}")

    # 2. DATABASE (Persistent Record)
  if level >= config.LEVEL_DB:
    anvil.server.launch_background_task(
      'persist_log_and_alert_async', 
      message, level, source, context, config.ACTIVE_ENV
    )

# --- BACKGROUND TASKS ---
@anvil.server.background_task
def persist_log_and_alert_async(message, level, source, context, environment):
  """
    This runs in a SEPARATE transaction. 
    Even if the bot crashes, this persists.
    """
  # 1. Write to DB
  try:
    data_str = json.dumps(context, default=str) if context else None
    app_tables.logs.add_row(
      timestamp=dt.datetime.now(),
      level=config.LOG_NAMES.get(level, "UNKNOWN"),
      source=source,
      message=message,
      data=data_str,
      environment=environment
    )
  except Exception as e:
    print(f"FAILED TO PERSIST LOG: {e}")

    # 2. If Critical, send the alert (Pushover)
  if level >= config.LEVEL_ALERT:
    send_alert_async(message, level, source) # Call your existing alert logic

@anvil.server.callable
@anvil.server.background_task
def send_alert_async(message, level, source):
  """
    Handles slow notifications.
    Optimized for Email-to-SMS Gateways (Plain text, short).
    """
  user_key = anvil.secrets.get_secret('PUSHOVER_USER')
  api_token = anvil.secrets.get_secret('PUSHOVER_TOKEN')

  if not user_key or not api_token:
    print("LOGGING ERROR: Missing Pushover Secrets.")
    return
  
    # Map Config Level to Pushover Priority
    # 0 = Normal, 1 = High, 2 = Emergency (Nag until acknowledge)
    priority = 0
  if level >= config.LOG_CRITICAL:
    priority = 1 # High Priority (Red color, bypass silent mode often)
  
    env_code = "P" if config.ACTIVE_ENV == config.ENV_PROD else "S"
  title = f"[{env_code}] {config.LOG_NAMES.get(level, 'ALERT')}: {source}"
  
  try:
    resp = requests.post(
      "https://api.pushover.net/1/messages.json",
      data={
        "token": api_token,
        "user": user_key,
        "message": message,
        "title": title,
        "priority": priority,
        # "sound": "siren", # Optional: Customize sound for criticals
      }
    )
  
    if resp.status_code == 200:
      print(f">> PUSHOVER SENT: {title}")
    else:
      print(f"PUSHOVER FAILED: {resp.text}")
  
  except Exception as e:
    print(f"FAILED TO SEND ALERT: {e}")
    
@anvil.server.callable
@anvil.server.background_task
def send_daily_digest():
  """
    Scans logs for the last 24 hours. 
    Sends an email summary of Warnings/Criticals.
    """
  print("LOG: Generating Daily Digest...")

  # 1. Define Time Window (Last 24h)
  now = dt.datetime.now()
  yesterday = now - dt.timedelta(days=1)

  # 2. Query Logs (Warnings and Criticals only)
  # Note: Anvil tables don't support >= on strings, so we filter in Python or use query operators if columns are numbers.
  # Since 'level' is stored as Text ("WARNING"), we filter by that.

  # Efficient Query: Filter by date first
  recent_logs = app_tables.logs.search(
    timestamp=q.greater_than(yesterday)
  )

  warnings = []
  criticals = []

  for row in recent_logs:
    if row['level'] == "WARNING":
      warnings.append(row)
    elif row['level'] == "CRITICAL":
      criticals.append(row)

  if not warnings and not criticals:
    print("LOG: No warnings found. Skipping email.")
    return

    # 3. Format Email Body
  lines = [f"Daily Automation Digest ({config.ACTIVE_ENV})", ""]

  if criticals:
    lines.append("=== CRITICAL ERRORS (ACTION REQUIRED) ===")
    for log in criticals:
      lines.append(f"[{log['timestamp'].strftime('%H:%M')}] {log['source']}: {log['message']}")
    lines.append("")

  if warnings:
    lines.append("=== WARNINGS (Review Needed) ===")
    for log in warnings:
      lines.append(f"[{log['timestamp'].strftime('%H:%M')}] {log['source']}: {log['message']}")

  email_body = "\n".join(lines)

  # 4. Send
  # Replace with your actual email address
  anvil.email.send(
    to=config.LOG_EMAIL,
    subject=f"Bot Digest: {len(criticals)} Criticals, {len(warnings)} Warnings",
    text=email_body
  )
  print("LOG: Digest Email Sent.")