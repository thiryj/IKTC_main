import anvil.email
import anvil.server
import anvil.tables as tables
from anvil.tables import app_tables
import anvil.tables.query as q
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
  # 1. Weekend Check (5=Saturday, 6=Sunday)
  if level < config.LOG_WARNING:
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
@anvil.server.callable
@anvil.server.background_task
def send_alert_async(message, level, source):
  """
    Handles slow notifications.
    Optimized for Email-to-SMS Gateways (Plain text, short).
    """
  # 1. Get Target
  target = anvil.secrets.get_secret('ALERT_SMS_EMAIL')
  if not target:
    print("LOGGING ERROR: No ALERT_SMS_EMAIL secret defined.")
    return
  print(f"SMS email is: {target}")

  # 2. Format for SMS (Keep it tiny)
  # Most gateways put the Subject in parenthesis: "(CRITICAL) Message..."
  # We strip the environment down to 1 char (P/S) to save space.
  env_code = "P" if config.ACTIVE_ENV == config.ENV_PROD else "S"
  lvl_name = config.LOG_NAMES.get(level, "CRITICAL")

  # Subject: [P] CRITICAL
  subject_line = f"[{env_code}] {lvl_name}"

  # Body: Source - Message
  # Truncate message to ~100 chars to avoid splitting SMS
  clean_msg = message[:100] + "..." if len(message) > 100 else message
  body_text = f"{source}: {clean_msg}"

  try:
    anvil.email.send(
      from_name="Iron Keep Trading Company",
      to='john@thiry.com',   #target,
      subject=subject_line,
      text=body_text # <--- Pure Text argument ensures no HTML bloat
    )
    print(f">> ASYNC SMS SENT: {subject_line} {body_text}")

  except Exception as e:
    print(f"FAILED TO SEND SMS ALERT: {e}")
    
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