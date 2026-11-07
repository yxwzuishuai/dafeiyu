import unittest
from decimal import Decimal as D

from dafeiyu.config import parse_config
from dafeiyu.engine import Monitor
from dafeiyu.models import Asset, Quote, Row
from dafeiyu.providers import Batch, FetchError
from dafeiyu.render import clip, quantity_text, render, width
from dafeiyu.scheduler import run_periodically


def sample_quote(kind, code):
    return Quote(kind, code, "测试", D("11.81"), D("0.17"), D("1.46"), None, None, "CNY", "2026-11-06", "测试源", "净值" if kind == "fund" else "现价")


class FakeProviders:
    def __init__(self):
        self.fund_calls = 0
        self.fail_stock = False

    def stocks(self, codes):
        if self.fail_stock:
            raise FetchError("股票连接失败")
        return Batch(quotes={("stock", code): sample_quote("stock", code) for code in codes})

    def funds(self, codes):
        self.fund_calls += 1
        return Batch(quotes={("fund", code): sample_quote("fund", code) for code in codes})

    def coin(self, code):
        raise FetchError("币种连接失败")

    def daily(self, code):
        return []


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.providers = FakeProviders()
        self.config = parse_config({"stocks": ["sz000001"], "funds": ["110013"], "coins": ["BTC-USD"]})
        self.monitor = Monitor(self.config, self.providers, clock=lambda: self.now)

    def test_one_failing_source_does_not_stop_other_categories(self):
        rows = self.monitor.refresh()
        self.assertEqual([row.asset.kind for row in rows], ["stock", "fund", "coin"])
        self.assertFalse(rows[0].error)
        self.assertFalse(rows[1].error)
        self.assertEqual(rows[2].error, "币种连接失败")

    def test_failed_refresh_keeps_prior_quote_and_marks_it_stale(self):
        original = self.monitor.refresh()[0].quote
        self.providers.fail_stock = True
        rows = self.monitor.refresh()
        self.assertIs(rows[0].quote, original)
        self.assertTrue(rows[0].stale)
        self.assertIn("更新失败/旧数据", render(rows, 10))
        self.providers.fail_stock = False
        self.assertFalse(self.monitor.refresh()[0].stale)

    def test_funds_use_their_own_request_interval(self):
        self.monitor.refresh()
        self.now = 10
        self.monitor.refresh()
        self.assertEqual(self.providers.fund_calls, 1)
        self.now = 60
        self.monitor.refresh()
        self.assertEqual(self.providers.fund_calls, 2)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class SchedulerTests(unittest.TestCase):
    def test_job_duration_does_not_accumulate_into_the_interval(self):
        clock = Clock()
        starts = []

        def job():
            starts.append(clock.now)
            clock.now += 3

        run_periodically(job, 10, 3, clock=clock, sleep=clock.sleep)
        self.assertEqual(starts, [0, 10, 20])

    def test_long_job_skips_missed_slots_without_overlap_or_catchup_storm(self):
        clock = Clock()
        starts = []

        def job():
            starts.append(clock.now)
            clock.now += 25

        run_periodically(job, 10, 3, clock=clock, sleep=clock.sleep)
        self.assertEqual(starts, [0, 30, 60])


class RenderTests(unittest.TestCase):
    def test_small_coin_price_and_quantity_are_not_rounded_to_zero(self):
        asset = Asset("coin", "DOGE-USD", D("0.00000001"), D("0.00000001"))
        quote = Quote("coin", "DOGE-USD", "测试", D("0.00000012"), None, None, None, None, "USD", "2026-11-06", "测试源")
        output = render([Row(asset, quote)], 10)
        self.assertIn("0.00000012", output)
        self.assertIn("0.00000001", output)
        self.assertEqual(quantity_text(D("1000")), "1,000")

    def test_missing_quote_is_not_displayed_as_zero(self):
        output = render([Row(Asset("stock", "sz000001"), None, "网络失败")], 10)
        self.assertIn("获取失败", output)
        self.assertIn("网络失败", output)
        self.assertNotIn("0.00", output)

    def test_published_nav_and_control_characters_are_visible_safely(self):
        output = render([Row(Asset("fund", "110013"), sample_quote("fund", "110013"))], 10)
        self.assertIn("基金/净值", output)
        self.assertEqual(width("A平安"), 5)
        self.assertLessEqual(width(clip("平安银行", 5)), 5)
        self.assertNotIn("\x1b", clip("\x1b[31m危险", 30))
