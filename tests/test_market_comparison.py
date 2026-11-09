import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest.mock import patch

from dafeiyu.market import (
    MARKET_TIMEZONE, MarketDataError, compare_market_days, compare_market_minutes,
    comparison_cutoff, parse_market_days, parse_market_minutes,
)
from dafeiyu.models import MarketTurnover
from dafeiyu.providers import FetchError, Providers
from dafeiyu.render import market_cell


NOW = datetime(2026, 11, 9, 9, 49, 18, tzinfo=MARKET_TIMEZONE)


def snapshot(stamp=NOW):
    return MarketTurnover(D(100000000), D(200000000), stamp, '东方财富')


def minute_payload(code):
    if code == 'sh000001':
        current = [('0930', '10000000'), ('0948', '90000000'), ('0949', '100000000'), ('1500', '300000000')]
        previous = [('0930', '15000000'), ('0948', '80000000'), ('0949', '90000000'), ('1500', '250000000')]
    else:
        current = [('0930', '20000000'), ('0948', '190000000'), ('0949', '200000000'), ('1500', '400000000')]
        previous = [('0930', '15000000'), ('0948', '150000000'), ('0949', '170000000'), ('1500', '350000000')]
    days = [{'date': day, 'data': [f'{minute} 100 999999 {amount}' for minute, amount in points]} for day, points in [('20261109', current), ('20261106', previous)]]
    return json.dumps({'code': 0, 'data': {code: {'data': days}}})


def daily_payload(code):
    current, previous = ('300000000', '250000000') if code == 'sh000001' else ('400000000', '350000000')
    return json.dumps({'rc': 0, 'data': {'code': code[2:], 'market': 1 if code.startswith('sh') else 0, 'klines': [f'2026-11-06,1,1,1,1,999999,{previous}', f'2026-11-09,1,1,1,1,999999,{current}']}})


def histories():
    return {code: parse_market_minutes(minute_payload(code), code) for code in ('sh000001', 'sz399106')}


class ComparisonTests(unittest.TestCase):
    def test_monday_uses_friday_and_aligns_the_amount_with_its_comparison_minute(self):
        result = compare_market_minutes(snapshot(), histories(), comparison_cutoff(snapshot(), NOW))
        self.assertEqual(result.previous_day, '2026-11-06')
        self.assertEqual(result.market_time.strftime('%H:%M'), '09:48')
        self.assertEqual(result.total_amount, D(280000000))
        self.assertEqual(result.previous_amount, D(230000000))
        self.assertEqual(result.change_amount, D(50000000))
        self.assertEqual(market_cell(result, '', NOW).plain, '2.80(同期增量0.50亿)')

    def test_missing_previous_minute_does_not_fall_back_to_an_earlier_time(self):
        history = histories()
        del history['sz399106']['2026-11-06']['0948']
        with self.assertRaisesRegex(MarketDataError, '对应分钟'):
            compare_market_minutes(snapshot(), history, comparison_cutoff(snapshot(), NOW))

    def test_missing_previous_trading_day_does_not_silently_skip_to_an_older_day(self):
        history = histories()
        history['sh000001']['2026-11-05'] = history['sh000001']['2026-11-06']
        history['sz399106']['2026-11-05'] = history['sz399106'].pop('2026-11-06')
        with self.assertRaisesRegex(MarketDataError, '上一交易日'):
            compare_market_minutes(snapshot(), history, comparison_cutoff(snapshot(), NOW))

    def test_late_current_minute_history_is_not_reported_as_current(self):
        with self.assertRaisesRegex(MarketDataError, '延迟'):
            compare_market_minutes(snapshot(), histories(), NOW.replace(hour=10, minute=0, second=0))

    def test_session_boundaries_respect_incomplete_minutes_and_noon_break(self):
        self.assertEqual(comparison_cutoff(snapshot(), NOW).strftime('%H%M'), '0948')
        noon = NOW.replace(hour=11, minute=30)
        self.assertEqual(comparison_cutoff(snapshot(noon), noon.replace(hour=12, minute=0)).strftime('%H%M'), '1130')
        close = NOW.replace(hour=15, minute=0)
        self.assertEqual(comparison_cutoff(snapshot(close), close+timedelta(seconds=5)).strftime('%H%M'), '1500')
        with self.assertRaises(MarketDataError):
            comparison_cutoff(snapshot(NOW.replace(minute=30)), NOW.replace(minute=30))

    def test_close_compares_daily_totals_instead_of_mixing_with_intraday_amounts(self):
        history = {code: parse_market_days(daily_payload(code), code) for code in ('sh000001', 'sz399106')}
        close = snapshot(NOW.replace(hour=15, minute=5))
        result = compare_market_days(close, history)
        self.assertEqual(result.total_amount, D(700000000))
        self.assertEqual(result.previous_amount, D(600000000))
        self.assertEqual(market_cell(result, '', close.market_time).plain, '7.00(全天增量1.00亿)')

    def test_decrease_flat_and_old_values_keep_explicit_labels(self):
        result = compare_market_minutes(snapshot(), histories(), comparison_cutoff(snapshot(), NOW))
        decrease = replace(result, previous_amount=D(350000000))
        self.assertEqual(market_cell(decrease, '', NOW).plain, '2.80(同期缩量0.70亿)')
        self.assertEqual(market_cell(replace(result, previous_amount=result.total_amount), '', NOW).plain, '2.80(同期持平)')
        self.assertEqual(market_cell(result, '断网', NOW).plain, '2.80(同期增量0.50亿/旧)')


class ComparisonParserTests(unittest.TestCase):
    def test_cumulative_minutes_are_not_summed_again(self):
        data = parse_market_minutes(minute_payload('sh000001'), 'sh000001')
        self.assertEqual(data['2026-11-06']['1500'], D(250000000))
        self.assertEqual(data['2026-11-09']['0948'], D(90000000))

    def test_wrong_symbols_bad_dates_and_invalid_cumulative_amounts_are_rejected(self):
        with self.assertRaises(MarketDataError):
            parse_market_minutes(minute_payload('sh000001'), 'sz399106')
        for change in ('bad-date', 'negative', 'backwards', 'duplicate'):
            payload = json.loads(minute_payload('sh000001'))
            day = payload['data']['sh000001']['data'][0]
            if change == 'bad-date':
                day['date'] = 'bad'
            elif change == 'negative':
                day['data'][0] = '0930 100 10 -1'
            elif change == 'backwards':
                day['data'][1] = '0948 100 10 1'
            else:
                day['data'].append(day['data'][0])
            with self.subTest(change=change), self.assertRaises(MarketDataError):
                parse_market_minutes(json.dumps(payload), 'sh000001')

    def test_daily_turnover_validates_the_market_and_uses_amount_not_volume(self):
        result = parse_market_days(daily_payload('sh000001'), 'sh000001')
        self.assertEqual(result['2026-11-06'], D(250000000))
        with self.assertRaises(MarketDataError):
            parse_market_days(daily_payload('sh000001'), 'sz399106')


class HistoryClient:
    def __init__(self):
        self.history_calls = 0
        self.snapshot_calls = 0
        self.fail_history = False
        self.stamp = NOW

    def get(self, url, **kwargs):
        if 'ulist.np' in url:
            self.snapshot_calls += 1
            return json.dumps({'rc': 0, 'data': {'diff': [{'f12': code, 'f13': market, 'f6': amount, 'f124': int(self.stamp.timestamp())} for code, market, amount in [('000001', 1, 100000000), ('399106', 0, 200000000)]]}})
        self.history_calls += 1
        if self.fail_history:
            raise FetchError('历史数据失败')
        if 'day/query' in url:
            return minute_payload('sh000001' if 'code=sh000001' in url else 'sz399106')
        return daily_payload('sh000001' if '1.000001' in url else 'sz399106')


class ComparisonCacheTests(unittest.TestCase):
    def test_history_is_shared_within_a_minute_and_updated_for_the_next_minute(self):
        client = HistoryClient()
        provider = Providers(client)
        first = provider.market_turnover(now=NOW)
        second = provider.market_turnover(now=NOW+timedelta(seconds=5))
        self.assertEqual(first.change_amount, second.change_amount)
        self.assertEqual(client.history_calls, 2)
        provider.market_turnover(now=NOW+timedelta(minutes=1))
        self.assertEqual(client.history_calls, 4)
        self.assertEqual(client.snapshot_calls, 3)

    def test_history_failure_keeps_fresh_total_without_an_invented_zero_difference(self):
        client = HistoryClient()
        client.fail_history = True
        result = Providers(client).market_turnover(now=NOW)
        self.assertEqual(result.total_amount, D(300000000))
        self.assertIsNone(result.change_amount)
        self.assertTrue(result.comparison_error)
        self.assertEqual(market_cell(result, '', NOW).plain, '3.00(对比--)')

    def test_failed_comparison_is_retried_after_a_minute_even_if_snapshot_time_is_frozen(self):
        client = HistoryClient()
        client.fail_history = True
        provider = Providers(client)
        with patch('dafeiyu.providers.time.monotonic', return_value=100):
            provider.market_turnover(now=NOW)
        failed_calls = client.history_calls
        client.fail_history = False
        with patch('dafeiyu.providers.time.monotonic', return_value=130):
            provider.market_turnover(now=NOW)
        self.assertEqual(client.history_calls, failed_calls)
        with patch('dafeiyu.providers.time.monotonic', return_value=161):
            recovered = provider.market_turnover(now=NOW)
        self.assertIsNotNone(recovered.change_amount)
        self.assertFalse(recovered.comparison_error)

    def test_after_close_uses_daily_history_and_caches_it(self):
        client = HistoryClient()
        client.stamp = NOW.replace(hour=15, minute=5)
        provider = Providers(client)
        result = provider.market_turnover(now=client.stamp)
        provider.market_turnover(now=client.stamp+timedelta(seconds=5))
        self.assertEqual(result.comparison_kind, '全天')
        self.assertEqual(result.change_amount, D(100000000))
        self.assertEqual(client.history_calls, 2)
