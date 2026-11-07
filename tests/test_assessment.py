import unittest
from dataclasses import replace
from decimal import Decimal as D

from rich.table import Table

from dafeiyu.assessment import assess, assess_row
from dafeiyu.models import Asset, DailyIndicators, Quote, Row
from dafeiyu.render import indicator_cells, render, render_view, width


def indicator(*, k='30', d='25', phase='金叉', tail=('.02', '.04', '.06'), baseline='.1', dea='.1', provisional=False):
    history = tuple([D(baseline)] * 17 + [D(value) for value in tail])
    k, d, dea = D(k), D(d), D(dea)
    current = history[-1]
    return DailyIndicators('2026-11-06', k, d, 3*k-2*d, dea+current/2, dea, current, phase, provisional, '红柱' if current > 0 else '绿柱' if current < 0 else '零轴', history)


def row_with(ind=None):
    quote = Quote('stock', 'sh588710', '科创半导体设备ETF华泰柏瑞', D('.725'), D('-.008'), D('-1.09'), D('.758'), D('.695'), 'CNY', '2026-11-06 16:14:58', '腾讯')
    return Row(Asset('stock', 'sh588710', D('.716'), D(30300)), quote, indicators=ind if ind is not None else indicator())


class AssessmentRulesTests(unittest.TestCase):
    def test_low_mid_gold_and_growing_red_above_zero_is_high(self):
        result = assess(indicator())
        self.assertEqual(result.label, '性价比高')
        self.assertTrue(result.reason)

    def test_oversold_with_expanding_green_and_death_is_not_a_buy_label(self):
        result = assess(indicator(k='10', d='16', phase='死叉', tail=('-.02', '-.04', '-.06')))
        self.assertEqual(result.label, '不建议买')

    def test_upcoming_death_and_shrinking_red_is_also_unfavorable(self):
        self.assertEqual(assess(indicator(k='60', d='55', phase='即将死叉', tail=('.1', '.08', '.06'))).label, '不建议买')

    def test_shrinking_green_and_low_upcoming_gold_is_only_trial(self):
        self.assertEqual(assess(indicator(k='12', d='14', phase='即将金叉', tail=('-.06', '-.04', '-.02'))).label, '小仓位')

    def test_gold_without_agreeing_macd_is_low(self):
        self.assertEqual(assess(indicator(tail=('-.02', '-.04', '-.06'))).label, '性价比低')
        self.assertEqual(assess(indicator(tail=('.1', '.08', '.06'))).label, '性价比低')

    def test_high_kdj_is_not_high_even_when_red_bars_expand(self):
        self.assertEqual(assess(indicator(k='90', d='84')).label, '性价比低')

    def test_rebound_below_the_zero_axis_does_not_receive_high(self):
        self.assertEqual(assess(indicator(dea='-.1')).label, '小仓位')

    def test_unusually_long_red_bar_does_not_receive_high(self):
        self.assertEqual(assess(indicator(tail=('.02', '.04', '.5'), baseline='.01')).label, '小仓位')

    def test_intraday_favorable_combination_is_capped_at_trial(self):
        self.assertEqual(assess(indicator(provisional=True)).label, '小仓位')

    def test_macd_price_scale_does_not_change_the_result(self):
        original = indicator()
        for factor in (D('.00001'), D(1), D(10000)):
            scaled = replace(original, dif=original.dif*factor, dea=original.dea*factor, macd=original.macd*factor, macd_history=tuple(value*factor for value in original.macd_history))
            self.assertEqual(assess(scaled).label, '性价比高')

    def test_missing_or_inconsistent_macd_history_does_not_invent_a_rating(self):
        self.assertEqual(assess(replace(indicator(), macd_history=())).label, '--')
        self.assertEqual(assess(replace(indicator(), macd=D('.3'))).label, '--')
        self.assertEqual(assess(replace(indicator(), k=D('NaN'))).label, '--')


class AssessmentDisplayTests(unittest.TestCase):
    def test_stale_or_failed_quotes_and_indicators_display_pending(self):
        original = row_with()
        for row in (replace(original, stale=True), replace(original, error='连接失败'), replace(original, indicators_stale=True), replace(original, indicator_error='日线失败'), replace(original, quote=None)):
            with self.subTest(row=row):
                self.assertEqual(assess_row(row).label, '待更新')
                self.assertEqual(indicator_cells(row)['rating'].plain, '待更新')
        self.assertEqual(assess_row(replace(original, indicators=None)).label, '--')

    def test_mismatched_source_dates_do_not_display_a_buy_label(self):
        original = row_with()
        for market_time in ('2026-11-09 10:00:00', '2026-11-05 15:00:00', '--'):
            row = replace(original, quote=replace(original.quote, market_time=market_time))
            self.assertEqual(assess_row(row).label, '待更新')

    def test_technical_rating_does_not_depend_on_the_users_holding_cost(self):
        original = row_with()
        other = replace(original, asset=replace(original.asset, cost_price=D('999'), quantity=D(0)))
        self.assertEqual(assess_row(original), assess_row(other))

    def test_rating_follows_macd_and_keeps_the_requested_short_names(self):
        row = row_with()
        table = next(part for part in render_view([row], 10, columns=180).renderables if isinstance(part, Table))
        headers = [column.header.plain for column in table.columns]
        self.assertEqual(headers[headers.index('MACD(日)') + 1], '性价比')
        for columns in (80, 100, 180):
            output = render([row], 10, columns=columns)
            self.assertIn('性价比高', output)
            self.assertIn('半导', output)
            self.assertTrue(all(width(line) <= columns for line in output.splitlines()))
        provisional = row_with(indicator(provisional=True))
        self.assertEqual(indicator_cells(provisional)['rating'].plain, '小仓位(预)')
