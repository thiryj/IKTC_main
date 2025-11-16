import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables
# This is a module.
# You can define variables and functions here, and use them from any form. For example, in a top-level form:
#
#    from .. import Module1
#
#    Module1.say_hello()
#

class LiveSettings:
  def __init__(self, row):
    # Use super to avoid recursion error when setting _row
    super().__setattr__("_row", row)

    # --- Dot Notation Support (for your code) ---
  def __getattr__(self, name):
    try:
      return self._row[name]
    except KeyError:
      raise AttributeError(name)

  def __setattr__(self, name, value):
    self._row[name] = value

    # --- Bracket Support (for Anvil Data Bindings) ---
  def __getitem__(self, key):
    return self._row[key]

  def __setitem__(self, key, value):
    self._row[key] = value
