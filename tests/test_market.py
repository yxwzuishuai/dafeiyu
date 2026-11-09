import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from rich.table import Table

from dafeiyu.config import parse_config
from dafeiyu.engine import Monitor
from dafeiyu.market import MARKET_TIMEZONE, MarketDataError, parse_market_eastmoney, parse_market_sina
from dafeiyu.models import Asset, MarketTurnover, Quote, Row
from dafeiyu.providers import Batch, FetchError, Providers
from dafeiyu.render import market_cell, render, render_view, width


NOW = datetime(2026, 11, 9, 9, 49, 18, tzinfo=MARKET_TIMEZONE)


def record(code, market, amount, stamp=NOW):
    return {'f12': code, 'f13': market, 'f6': amount, 'f5': 999999999999, 'f124': int(stamp.timestamp())}


def em_payload(records=None):
    if records is None:
        records = [record('000001', 1, '100000000'), record('399106', 0, '200000000')]
    return json.dumps({'rc': 0, 'data': {'diff': records}})


def sina_line(code, amount, stamp=NOW):
    values = ['0'] * 32
    values[0], values[8], values[9] = '指数', '999999999999', str(amount)
    values[30], values[31] = stamp.strftime('%Y-%m-%d'), stamp.strftime('%H:%M:%S')
    return f'var hq_str_{code}="' + ','.join(values) + '";'


def sample_row(code='sh588710'):
    return Row(Asset('stock', code), Quote('stock', code, '测试', D(10), D(0), D(0), D(11), D(9), 'CNY', '2026-11-09 09:49:18', '测试源'))


class MarketParsingTests(unittest.TestCase):
    def test_sources_agree_and_amount_is_not_confused_with_share_volume(self):
        em = parse_market_eastmoney(em_payload())
        sina = parse_market_sina(sina_line('sh000001', '100000000') + sina_line('sz399106', '200000000'))
        self.assertEqual(em.total_amount, D(300000000))
        self.assertEqual(em.total_amount, sina.total_amount)
        self.assertEqual(em.market_time, NOW)
        self.assertEqual(market_cell(em, '', NOW).plain, '3.00(对比--)')

    def test_missing_one_market_and_using_a_different_index_are_rejected(self):
        for records in ([record('000001', 1, 1)], [record('000001', 1, 1), record('399001', 0, 2)]):
            with self.subTest(records=records), self.assertRaises(MarketDataError):
                parse_market_eastmoney(em_payload(records))
        with self.assertRaises(MarketDataError):
            parse_market_sina(sina_line('sh000001', '100000000'))

    def test_duplicate_indices_are_rejected_and_unrelated_indices_are_not_added(self):
        records = [record('000001', 1, 1), record('399106', 0, 2)]
        with self.assertRaisesRegex(MarketDataError, '重复'):
            parse_market_eastmoney(em_payload([*records, records[0]]))
        result = parse_market_eastmoney(em_payload([*records, record('399006', 0, 1000)]))
        self.assertEqual(result.total_amount, D(3))

    def test_same_trading_date_and_reasonably_close_timestamps_are_required(self):
        with self.assertRaisesRegex(MarketDataError, '日期'):
            parse_market_eastmoney(em_payload([record('000001', 1, 1), record('399106', 0, 2, NOW-timedelta(days=3))]))
        with self.assertRaisesRegex(MarketDataError, '2 分钟'):
            parse_market_eastmoney(em_payload([record('000001', 1, 1), record('399106', 0, 2, NOW-timedelta(seconds=121))]))
        result = parse_market_eastmoney(em_payload([record('000001', 1, 1), record('399106', 0, 2, NOW-timedelta(seconds=120))]))
        self.assertEqual(result.market_time, NOW-timedelta(seconds=120))

    def test_invalid_or_missing_values_do_not_become_zero(self):
        for bad in (None, '--', 'NaN', '-1', True):
            with self.subTest(bad=bad), self.assertRaises(MarketDataError):
                parse_market_eastmoney(em_payload([record('000001', 1, bad), record('399106', 0, 2)]))
        for bad in (None, '--', 'NaN', '-1'):
            with self.subTest(bad=bad), self.assertRaises(MarketDataError):
                parse_market_sina(sina_line('sh000001', bad) + sina_line('sz399106', 2))
        self.assertEqual(parse_market_eastmoney(em_payload([record('000001', 1, 0), record('399106', 0, 0)])).total_amount, D(0))

    def test_bad_timestamps_or_response_shapes_fail_cleanly(self):
        for stamp in (None, 'bad', -1, 1.5, True):
            broken = record('000001', 1, 1)
            broken['f124'] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(MarketDataError):
                parse_market_eastmoney(em_payload([broken, record('399106', 0, 2)]))
        for payload in ('not json', '[]', '{"rc":0,"data":null}', em_payload([None])):
            with self.subTest(payload=payload), self.assertRaises(MarketDataError):
                parse_market_eastmoney(payload)

    def test_fallback_uses_a_complete_pair_from_one_source(self):
        class Client:
            def __init__(self):
                self.urls = []

            def get(self, url, **kwargs):
                self.urls.append(url)
                if len(self.urls) == 1:
                    return em_payload([record('000001', 1, '999999999')])
                return sina_line('sh000001', '100000000') + sina_line('sz399106', '200000000')
        client = Client()
        result = Providers(client).market_turnover()
        self.assertEqual(result.source, '新浪')
        self.assertEqual(result.total_amount, D(300000000))
        self.assertEqual(len([url for url in client.urls if 'day/query' not in url]), 2)
        self.assertIn('399106', client.urls[0])
        self.assertNotIn('399001', client.urls[0])


class MarketMonitorTests(unittest.TestCase):
    def test_one_market_job_per_cycle_is_shared_and_failures_do_not_stop_quotes(self):
        class FakeProviders:
            def __init__(self):
                self.market_calls = 0
                self.quote_calls = 0
                self.fail_market = False

            def stocks(self, codes):
                self.quote_calls += 1
                return Batch(quotes={('stock', code): replace(sample_row(code).quote, price=D(10+self.quote_calls)) for code in codes})

            def daily(self, code):
                return []

            def market_turnover(self):
                self.market_calls += 1
                if self.fail_market:
                    raise FetchError('市场连接失败')
                return parse_market_eastmoney(em_payload())
        providers = FakeProviders()
        monitor = Monitor(parse_config({'stocks': ['sh588710', 'sz002384']}), providers)
        first = monitor.refresh()
        cached = monitor.market_turnover
        self.assertEqual(providers.market_calls, 1)
        self.assertEqual(cached.total_amount, D(300000000))
        providers.fail_market = True
        second = monitor.refresh()
        self.assertEqual(providers.market_calls, 2)
        self.assertIs(monitor.market_turnover, cached)
        self.assertEqual(monitor.market_error, '市场连接失败')
        self.assertTrue(all(not row.error for row in second))
        self.assertGreater(second[0].quote.price, first[0].quote.price)
        providers.fail_market = False
        monitor.refresh()
        self.assertFalse(monitor.market_error)


class MarketDisplayTests(unittest.TestCase):
    def test_market_summary_is_shown_once_above_the_table(self):
        market = parse_market_eastmoney(em_payload())
        rows = [sample_row(), sample_row('sz002384')]
        view = render_view(rows, 10, now=NOW, columns=180, market=market)
        table = next(part for part in view.renderables if isinstance(part, Table))
        headers = [column.header.plain for column in table.columns]
        self.assertIn('技术分', headers)
        self.assertNotIn('大A量能(亿)', headers)
        output = render(rows, 10, now=NOW, columns=180, market=market)
        self.assertEqual(output.count('3.00亿元'), 1)
        self.assertLess(output.index('沪深成交额'), output.index('代码'))
        for columns in (80, 100, 180):
            output = render(rows, 10, now=NOW, columns=columns, market=market)
            self.assertIn('3.00', output)
            self.assertTrue(all(width(line) <= columns for line in output.splitlines()))

    def test_missing_failed_and_previous_day_values_are_explicit(self):
        market = parse_market_eastmoney(em_payload())
        self.assertEqual(market_cell(None, '失败', NOW).plain, '--')
        self.assertEqual(market_cell(market, '失败', NOW).plain, '3.00(对比--/旧)')
        previous = replace(market, market_time=NOW-timedelta(days=3))
        self.assertEqual(market_cell(previous, '失败', NOW).plain, '3.00(对比--/11-06/旧)')
        output = render([sample_row(), sample_row('sz002384')], 10, now=NOW, columns=180, market=market, market_error='失败')
        self.assertEqual(output.count('! 大A量能：失败'), 1)

    def test_market_date_is_compared_in_china_time(self):
        market = parse_market_eastmoney(em_payload())
        instant = NOW.astimezone(timezone.utc)
        self.assertEqual(market_cell(market, '', instant).plain, '3.00(对比--)')
