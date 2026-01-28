from ._anvil_designer import form_shellTemplate
from anvil import *
import anvil.server
import anvil.tables as tables
import anvil.tables.query as q
from anvil.tables import app_tables

from ..form_main import form_main
from ..form_trade_editor import form_trade_editor
from ..form_stats import form_stats


class form_shell(form_shellTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.content_panel.add_component(form_main())

    # Any code you write here will run before the form opens.

  @handle("link_main", "click")
  def link_main_click(self, **event_args):
    """This method is called when the link is clicked"""
    self.content_panel.clear()
    self.content_panel.add_component(form_main()) # Swap with form_trade_editor() for the second link

  @handle("link_crud", "click")
  def link_crud_click(self, **event_args):
    """This method is called when the link is clicked"""
    self.content_panel.clear()
    self.content_panel.add_component(form_trade_editor()) # Swap with form_trade_editor() for the second link

  @handle("link_stats", "click")
  def link_stats_click(self, **event_args):
    """This method is called when the link is clicked"""
    self.content_panel.clear()
    self.content_panel.add_component(form_stats())
