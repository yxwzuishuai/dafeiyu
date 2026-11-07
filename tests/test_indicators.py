import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from rich.table import Table

from dafeiyu.config import parse_config
from dafeiyu.engine import Monitor
from dafeiyu.indicators import CHINA_TIME, calculate_daily
from dafeiyu.models import Asset, DailyBar, Quote, Row
from dafeiyu.providers import Batch, FetchError, Providers, parse_daily_bars, parse_eastmoney_daily, parse_tencent_daily
from dafeiyu.render import indicator_cells, render, render_view, width


def daily_bars(closes=None, *, count=40):
    closes = closes if closes is not None else [D(10)] * count
    bars = []
    day = date(2025, 1, 2)
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(DailyBar(day.isoformat(), D(11), D(9), D(close)))
        day += timedelta(days=1)
    return bars


class CalculationTests(unittest.TestCase):
    def test_gold_cross_and_macd_use_known_one_unit_step_from_flat_prices(self):
        bars = daily_bars([D(10)] * 39 + [D(11)])
        result = calculate_daily(bars)
        self.assertEqual(result.kdj_signal, "金叉")
        self.assertAlmostEqual(result.k, D(200) / 3, places=20)
        self.assertAlmostEqual(result.d, D(500) / 9, places=20)
        self.assertAlmostEqual(result.j, D(800) / 9, places=20)
        self.assertAlmostEqual(result.dif, D(2) / 13 - D(2) / 27, places=20)
        self.assertAlmostEqual(result.dea, result.dif / 5, places=20)
        self.assertAlmostEqual(result.macd, result.dif * D("1.6"), places=20)
        self.assertEqual(result.macd_color, "红柱")

    def test_death_cross_on_a_falling_step(self):
        result = calculate_daily(daily_bars([D(10)] * 39 + [D(9)]))
        self.assertEqual(result.kdj_signal, "死叉")
        self.assertLess(result.k, result.d)
        self.assertEqual(result.macd_color, "绿柱")

    def test_remaining_above_d_is_not_reported_as_another_gold_cross(self):
        result = calculate_daily(daily_bars([D(10)] * 38 + [D(11), D("10.9")]))
        self.assertGreater(result.k, result.d)
        self.assertEqual(result.kdj_signal, "无新交叉")
        # The price fell from 11 to 10.9, but its MACD histogram is still positive.
        self.assertEqual(result.macd_color, "红柱")

    def test_flat_high_low_window_does_not_divide_by_zero_or_invent_a_signal(self):
        bars = [replace(bar, high=D(10), low=D(10)) for bar in daily_bars()]
        result = calculate_daily(bars)
        self.assertEqual((result.k, result.d, result.j), (D(50), D(50), D(50)))
        self.assertEqual((result.dif, result.dea, result.macd), (D(0), D(0), D(0)))
        self.assertEqual(result.kdj_signal, "无新交叉")
        self.assertEqual(result.macd_color, "零轴")

    def test_short_or_repeated_history_does_not_produce_a_trading_signal(self):
        with self.assertRaisesRegex(ValueError, "不足"):
            calculate_daily(daily_bars(count=33))
        bars = daily_bars()
        with self.assertRaisesRegex(ValueError, "重复"):
            calculate_daily([*bars, bars[-1]])

    def test_intraday_signal_is_provisional_until_market_close(self):
        bars = daily_bars()
        today = datetime.fromisoformat(bars[-1].day).replace(tzinfo=CHINA_TIME)
        self.assertTrue(calculate_daily(bars, now=today.replace(hour=14, minute=59)).provisional)
        self.assertFalse(calculate_daily(bars, now=today.replace(hour=15)).provisional)
        self.assertFalse(calculate_daily(bars, now=today + timedelta(days=1)).provisional)
        with self.assertRaisesRegex(ValueError, "晚于"):
            calculate_daily(bars, now=today - timedelta(days=1))


class DailyProviderTests(unittest.TestCase):
    records = [["2026-11-06", "0.700", "0.725", "0.758", "0.695", "30262569"]]

    def test_tencent_and_eastmoney_normalize_to_the_same_daily_bar(self):
        tencent = json.dumps({"code": 0, "data": {"sh588710": {"qfqday": self.records}}})
        eastmoney = json.dumps({"rc": 0, "data": {"code": "588710", "klines": [",".join(self.records[0])]}})
        self.assertEqual(parse_tencent_daily(tencent, "sh588710"), parse_eastmoney_daily(eastmoney, "sh588710"))
        bar = parse_tencent_daily(tencent, "sh588710")[0]
        self.assertEqual((bar.close, bar.high, bar.low), (D("0.725"), D("0.758"), D("0.695")))

    def test_invalid_rows_are_rejected_rather_than_silently_shortening_history(self):
        for record in (
            ["bad-date", "1", "1", "2", "1"],
            ["2026-11-06", "1", "--", "2", "1"],
            ["2026-11-06", "1", "NaN", "2", "1"],
            ["2026-11-06", "1", "3", "2", "1"],
            ["2026-11-06", "1", "1", "0.5", "1"],
        ):
            with self.subTest(record=record), self.assertRaises(FetchError):
                parse_daily_bars([record])
        with self.assertRaisesRegex(FetchError, "重复"):
            parse_daily_bars(self.records * 2)

    def test_wrong_symbol_or_unadjusted_only_data_is_not_used(self):
        with self.assertRaises(FetchError):
            parse_tencent_daily(json.dumps({"code": 0, "data": {"sh588710": {"day": self.records}}}), "sh588710")
        with self.assertRaises(FetchError):
            parse_eastmoney_daily(json.dumps({"rc": 0, "data": {"code": "600519", "klines": [",".join(self.records[0])]}}), "sh588710")

    def test_history_uses_an_adjusted_backup_when_primary_fails(self):
        records = self.records
        class Client:
            def __init__(self):
                self.urls = []

            def get(self, url, **kwargs):
                self.urls.append(url)
                if len(self.urls) == 1:
                    raise FetchError("连接失败")
                return json.dumps({"rc": 0, "data": {"code": "588710", "klines": [",".join(records[0])]}})
        client = Client()
        self.assertEqual(Providers(client).daily("sh588710")[0].close, D("0.725"))
        self.assertIn("fqt=1", client.urls[-1])
        self.assertIn("secid=1.588710", client.urls[-1])


class IndicatorEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        class FakeProviders:
            def __init__(self):
                self.daily_calls = 0
                self.fail_daily = False

            def stocks(self, codes):
                return Batch(quotes={('stock', code): Quote('stock', code, '测试', D(10), D(0), D(0), D(11), D(9), 'CNY', '2025-02-26 15:00:00', '测试源') for code in codes})

            def daily(self, code):
                self.daily_calls += 1
                if self.fail_daily:
                    raise FetchError("日线失败")
                return daily_bars()
        self.providers = FakeProviders()
        self.monitor = Monitor(parse_config({'stocks': ['sh588710']}), self.providers, clock=lambda: self.now)

    def test_indicators_are_cached_between_ten_second_quote_refreshes(self):
        first = self.monitor.refresh()[0].indicators
        self.now = 10
        self.assertIs(self.monitor.refresh()[0].indicators, first)
        self.assertEqual(self.providers.daily_calls, 1)
        self.now = 60
        self.monitor.refresh()
        self.assertEqual(self.providers.daily_calls, 2)

    def test_daily_failure_retains_marked_old_indicators_without_failing_quotes(self):
        first = self.monitor.refresh()[0].indicators
        self.providers.fail_daily = True
        self.now = 60
        row = self.monitor.refresh()[0]
        self.assertIs(row.indicators, first)
        self.assertTrue(row.indicators_stale)
        self.assertEqual(row.indicator_error, "日线失败")
        self.assertFalse(row.error)
        self.assertFalse(row.stale)
        self.now = 70
        self.monitor.refresh()
        self.assertEqual(self.providers.daily_calls, 2)
        self.providers.fail_daily = False
        self.now = 120
        self.assertFalse(self.monitor.refresh()[0].indicators_stale)

    def test_first_daily_failure_has_no_fabricated_signal(self):
        self.providers.fail_daily = True
        row = self.monitor.refresh()[0]
        self.assertIsNone(row.indicators)
        self.assertFalse(row.indicators_stale)
        self.assertEqual(indicator_cells(row)['macd'].plain, '--')
        self.assertFalse(row.error)

    def test_unsupported_market_keeps_quotes_without_requesting_daily_data(self):
        monitor = Monitor(parse_config({'stocks': ['usAAPL']}), self.providers)
        row = monitor.refresh()[0]
        self.assertIsNotNone(row.quote)
        self.assertIsNone(row.indicators)
        self.assertFalse(row.indicator_error)
        self.assertEqual(self.providers.daily_calls, 0)


class IndicatorRenderTests(unittest.TestCase):
    def row(self):
        indicator = calculate_daily(daily_bars([D(10)] * 39 + [D(11)]))
        return Row(Asset('stock', 'sh588710', D(10), D(100)), Quote('stock', 'sh588710', '测试ETF', D(11), D(1), D(10), D(11), D(9), 'CNY', '2025-02-26 15:00:00', '测试源'), indicators=indicator)

    def test_signal_columns_immediately_follow_income_and_are_not_truncated(self):
        row = self.row()
        table = next(part for part in render_view([row], 10, columns=180).renderables if isinstance(part, Table))
        headers = [column.header.plain for column in table.columns]
        index = headers.index('收益')
        self.assertEqual(headers[index + 1:index + 4], ['KDJ(日)', 'MACD(日)', '指标日期'])
        for columns in (80, 100, 180):
            self.assertTrue(any(isinstance(part, Table) for part in render_view([row], 10, columns=columns).renderables))
            output = render([row], 10, columns=columns)
            self.assertIn('金叉', output)
            self.assertIn('红柱', output)
            self.assertTrue(all(width(line) <= columns for line in output.splitlines()))
        self.assertEqual(indicator_cells(row)['kdj'].style, 'red')
        self.assertEqual(indicator_cells(row)['macd'].style, 'red')

    def test_provisional_and_stale_signals_are_explicit(self):
        row = self.row()
        row = replace(row, indicators=replace(row.indicators, provisional=True), indicators_stale=True, indicator_error='日线失败')
        output = render([row], 10, columns=180)
        self.assertIn('金叉(预/旧)', output)
        self.assertIn('红柱(预/旧)', output)
        self.assertIn('日线失败', output)
        self.assertIn(row.indicators.as_of, output)
