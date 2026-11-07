import unittest
from datetime import datetime, timezone
from decimal import Decimal as D

from rich.table import Table

from dafeiyu.models import Asset, Quote, Row
from dafeiyu.render import render, render_view, width


NOW = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)


def etf_row(*, cost=True, stale=False, name="科创半导体设备ETF华泰柏瑞"):
    asset = Asset("stock", "sh588710", D("0.716") if cost else None, D("30300") if cost else None)
    quote = Quote("stock", "sh588710", name, D("0.725"), D("-0.008"), D("-1.09"), D("0.758"), D("0.695"), "CNY", "2026-11-06 16:14:58", "腾讯")
    return Row(asset, quote, "网络失败" if stale else "", stale)


class CombinedTableTests(unittest.TestCase):
    def test_quotes_and_positions_share_one_table_and_one_row_per_asset(self):
        rows = [etf_row(), etf_row(cost=False, name="另一只自选")]
        view = render_view(rows, 10, now=NOW, columns=160)
        tables = [part for part in view.renderables if isinstance(part, Table)]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].row_count, 2)
        output = render(rows, 10, now=NOW, columns=160)
        for value in ("涨跌幅", "最高", "最低", "成本价", "持仓", "收益率", "行情时间", "0.725", "0.716", "30,300", "+272.70", "+1.26%", "-1.09%"):
            self.assertIn(value, output)
        self.assertNotIn("行情明细", output)
        self.assertNotIn("持仓收益", output)

    def test_narrow_window_preserves_numbers_and_complete_names(self):
        for columns in (80, 100, 140):
            with self.subTest(columns=columns):
                output = render([etf_row()], 10, now=NOW, columns=columns)
                self.assertTrue(all(width(line) <= columns for line in output.splitlines()))
                for value in ("科创半导体设备ETF华泰柏瑞", "0.758", "0.695", "-0.008", "30,300", "+272.70"):
                    self.assertIn(value, output)

    def test_source_time_remains_distinct_from_refresh_time(self):
        output = render([etf_row()], 10, now=NOW, columns=160)
        self.assertIn("16:14:58", output)
        self.assertIn("2026-11-07", output)

    def test_mixed_currencies_and_dates_are_not_hidden(self):
        quote = Quote("coin", "BTC-USD", "Bitcoin", D("70200"), D("200"), D("0.29"), None, None, "USD", "2026-11-07 08:00:00Z", "雅虎")
        output = render([etf_row(), Row(Asset("coin", "BTC-USD"), quote)], 10, now=NOW, columns=200)
        for value in ("币种", "CNY", "USD", "2026-11-06 16:14:58", "2026-11-07 08:00:00Z", "UTC"):
            self.assertIn(value, output)

    def test_stale_quote_remains_explicit_alongside_estimated_income(self):
        output = render([etf_row(stale=True)], 10, now=NOW, columns=160)
        self.assertIn("更新失败/旧数据", output)
        self.assertIn("网络失败", output)
        self.assertIn("+272.70", output)

    def test_quote_markup_is_literal_and_plain_snapshot_has_no_escape_codes(self):
        output = render([etf_row(name="[bold]literal[/bold]\x1b")], 10, now=NOW, columns=160)
        self.assertIn("[bold]literal[/bold]", output)
        self.assertNotIn("\x1b", output)

    def test_very_narrow_window_falls_back_without_discarding_values(self):
        output = render([etf_row()], 10, now=NOW, columns=32)
        self.assertIn("逐项展示", output)
        for value in ("0.725", "0.716", "30,300", "+272.70", "16:14:58"):
            self.assertIn(value, output)
        self.assertTrue(all(width(line) <= 32 for line in output.splitlines()))
