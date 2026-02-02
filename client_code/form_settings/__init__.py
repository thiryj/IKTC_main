from ._anvil_designer import form_settingsTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables


class form_settings(form_settingsTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.load_data()

  def load_data(self):
    s = anvil.server.call('get_settings_for_editor')
    self.text_equity.text = s.get('total_account_equity')
    self.text_timeout.text = s.get('order_timeout_seconds')
    self.check_dry_run.checked = s.get('dry_run', False)
    self.check_enforce_hours.checked = s.get('enforce_trading_hours', True)

  def button_save_click(self, **event_args):
    new_data = {
      'total_account_equity': self.text_equity.text,
      'order_timeout_seconds': self.text_timeout.text,
      'dry_run': self.check_dry_run.checked,
      'enforce_trading_hours': self.check_enforce_hours.checked
    }
    anvil.server.call('save_settings', new_data)
    Notification("Settings Saved Successfully").show()
