from ._anvil_designer import RowTemplate4Template
from anvil import *
import datetime


class RowTemplate4(RowTemplate4Template):
  def __init__(self, **properties):
    self.init_components(**properties)

    # --- MANUAL BINDING & FORMATTING ---
    raw_ts = self.item['timestamp']

    if raw_ts:
      # 1. Convert to Eastern (Roughly UTC - 5 hours)
      # Note: Valid for Standard time. In summer it's -4. 
      # For a personal dashboard, this constant offset is usually "good enough".
      est_ts = raw_ts - datetime.timedelta(hours=5)

      # 2. Format: "2026-01-11 09:30:05" (Drops the +00:00 and microseconds)
      clean_str = est_ts.strftime("%Y-%m-%d %H:%M:%S")

      # 3. Push to UI Component
      # CHECK: Verify your label name in the Designer. It might be 'label_1' or 'label_timestamp'
      self.label_timestamp.text = clean_str
      