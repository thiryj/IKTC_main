import anvil.server
from anvil import *
from ._anvil_designer import form_settingsTemplate

class form_settings(form_settingsTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.refresh_ui_data()

  def refresh_ui_data(self) -> None:
    """Loads current settings from DB and populates the form."""
    s = anvil.server.call('get_live_settings')

    # Numbers
    self.text_equity.text = s.get('total_account_equity', 40000)
    self.text_ui_refresh.text = s.get('ui_refresh_seconds', 60)

    # Checkboxes
    self.check_dry_run.checked = s.get('dry_run', False)
    self.check_trading_hours.checked = s.get('enforce_trading_hours', True)
    self.check_late_open.checked = s.get('enforce_late_open_guardrail', True)
    self.check_frequency.checked = s.get('enforce_frequency_checks', True)
    self.check_zombies.checked = s.get('enforce_zombie_checks', True)
    self.check_consistency.checked = s.get('enforce_consistency_checks', False)
    self.check_pause_entries.checked = s.get('pause_new_entries', False)

  @handle("button_save", "click")
  def button_save_click(self, **event_args) -> None:
    """Bundles UI state and persists to server."""
    settings_bundle = {
      'total_account_equity': self.text_equity.text,
      'ui_refresh_seconds': self.text_ui_refresh.text,
      'dry_run': self.check_dry_run.checked,
      'enforce_trading_hours': self.check_trading_hours.checked,
      'enforce_late_open_guardrail': self.check_late_open.checked,
      'enforce_frequency_checks': self.check_frequency.checked,
      'enforce_zombie_checks': self.check_zombies.checked,
      'enforce_consistency_checks': self.check_consistency.checked,
      'pause_new_entries': self.check_pause_entries.checked
    }

    success = anvil.server.call('save_live_settings', settings_bundle)
    if success:
      Notification("Settings Saved Successfully", style="success").show()
    else:
      alert("Error saving settings.")