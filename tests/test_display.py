import unittest
from io import StringIO

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from dafeiyu.display import Dashboard


class DashboardTests(unittest.TestCase):
    def console(self, *, terminal=False):
        output = StringIO()
        console = Console(file=output, width=100, height=30, force_terminal=terminal, legacy_windows=False, color_system=None, _environ={"TERM": "xterm-256color"})
        return console, output

    def test_nonterminal_auto_mode_keeps_output_readable_and_explains_terminal(self):
        console, output = self.console()
        with Dashboard(console=console) as display:
            self.assertFalse(display.enabled)
            display.publish("FIRST SNAPSHOT")
            display.publish("SECOND SNAPSHOT")
        text = output.getvalue()
        self.assertIn("Terminal", text)
        self.assertIn("FIRST SNAPSHOT", text)
        self.assertIn("SECOND SNAPSHOT", text)
        self.assertNotIn("\x1b", text)

    def test_once_is_a_static_snapshot_even_in_live_mode(self):
        console, output = self.console(terminal=True)
        with Dashboard("live", single=True, console=console) as display:
            self.assertFalse(display.enabled)
            display.publish("ONE SNAPSHOT")
        text = output.getvalue()
        self.assertIn("ONE SNAPSHOT", text)
        self.assertNotIn("\x1b[?1049h", text)

    def test_dumb_terminal_falls_back_instead_of_showing_a_blank_screen(self):
        output = StringIO()
        console = Console(file=output, force_terminal=True, _environ={"TERM": "dumb"})
        with Dashboard("live", console=console) as display:
            self.assertFalse(display.enabled)
            display.publish("VISIBLE SNAPSHOT")
        self.assertIn("VISIBLE SNAPSHOT", output.getvalue())

    def test_live_restores_screen_and_cursor_after_interrupt(self):
        console, output = self.console(terminal=True)
        with self.assertRaises(KeyboardInterrupt):
            with Dashboard("live", console=console) as display:
                display.publish("LATEST QUOTE")
                raise KeyboardInterrupt
        text = output.getvalue()
        self.assertIn("\x1b[?1049h", text)
        self.assertIn("\x1b[?1049l", text)
        self.assertIn("\x1b[?25l", text)
        self.assertIn("\x1b[?25h", text)

    def test_refresh_keeps_prior_quote_and_does_not_parse_quote_markup(self):
        console, output = self.console(terminal=True)
        with Dashboard("live", console=console) as display:
            display.publish("[bold]literal-name[/bold] 0.725")
            display.begin_refresh()
            display.publish("[bold]literal-name[/bold] 0.726")
        text = output.getvalue()
        self.assertIn("[bold]literal-name[/bold]", text)
        self.assertIn("0.725", text)
        self.assertIn("0.726", text)

    def test_live_refresh_accepts_a_rich_table_without_printing_its_object_repr(self):
        console, output = self.console(terminal=True)
        table = Table("代码", "现价")
        table.add_row(Text("[bold]literal[/bold]"), "0.725")
        with Dashboard("live", console=console) as display:
            display.publish(Group(Text("大飞鱼行情"), table))
            display.begin_refresh()
        text = output.getvalue()
        self.assertIn("[bold]literal[/bold]", text)
        self.assertIn("0.725", text)
        self.assertNotIn("正在更新行情", text)
        self.assertNotIn("object at", text)
