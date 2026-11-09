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
    def test_same_phase_can_have_distinct_tenth_scores(self):
        results = [assess(indicator(k=str(k), d='25')) for k in (26, 27, 28, 29, 30)]
        scores = [result.score for result in results]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(len(set(scores)), 5)
        self.assertTrue(any(score != score.to_integral_value() for score in scores))
        self.assertTrue(all(result.label.endswith(str(result.score)[-2:]) for result in results))

    def test_strong_alignment_can_exceed_six_even_intraday(self):
        strong = indicator(k='20', d='0', tail=('0', '.1', '.2'), dea='.3', provisional=True)
        result = assess(strong)
        self.assertEqual(result.score, D('10.0'))
        self.assertEqual(result.label, '+10.0')
        self.assertEqual(result, assess(replace(strong, provisional=False)))
        self.assertNotIn('上限', result.reason)

    def test_kdj_crossing_does_not_create_a_fixed_six_point_jump(self):
        before = assess(indicator(k='49.99', d='50.01', phase='死叉'))
        after = assess(indicator(k='50.01', d='49.99'))
        self.assertLessEqual(abs(after.score - before.score), D('.2'))

    def test_phase_labels_do_not_override_numeric_strength(self):
        original = indicator()
        for phase in ('金叉', '即将金叉', '即将死叉', '死叉', '重合'):
            self.assertEqual(assess(replace(original, kdj_signal=phase)), assess(original))

    def test_oversold_with_expanding_green_and_death_is_not_rewarded(self):
        result = assess(indicator(k='10', d='16', phase='死叉', tail=('-.02', '-.04', '-.06'), dea='-.1'))
        self.assertLess(result.score, 0)
        self.assertEqual(dict(result.components)['KDJ位置'], 0)

    def test_recent_green_shrinkage_counts_even_after_an_opposite_day(self):
        result = assess(indicator(tail=('-.10', '-.12', '-.08')))
        self.assertGreater(dict(result.components)['MACD变化'], 0)
        widening = assess(indicator(tail=('-.10', '-.12', '-.16')))
        self.assertLess(dict(widening.components)['MACD变化'], 0)

    def test_macd_change_strength_is_graded_instead_of_fixed_two_points(self):
        weak = assess(indicator(tail=('.02', '.04', '.045')))
        strong = assess(indicator(tail=('.02', '.04', '.08')))
        self.assertLess(dict(weak.components)['MACD变化'], dict(strong.components)['MACD变化'])
        self.assertLess(dict(weak.components)['MACD变化'], 2)

    def test_conflicting_macd_conditions_reduce_the_score(self):
        healthy = assess(indicator()).score
        fading = assess(indicator(tail=('.1', '.08', '.06'))).score
        worsening = assess(indicator(tail=('-.02', '-.04', '-.06'))).score
        self.assertLess(worsening, fading)
        self.assertLess(fading, healthy)

    def test_overbought_penalty_increases_gradually_without_capping_total(self):
        readings = [assess(indicator(k=str(k), d=str(k-5))) for k in (75, 80, 85, 90)]
        position_scores = [dict(result.components)['KDJ位置'] for result in readings]
        self.assertEqual(position_scores, sorted(position_scores, reverse=True))
        self.assertEqual(len(set(position_scores)), 4)
        self.assertTrue(all('上限' not in result.reason for result in readings))

    def test_zero_axis_contributes_gradually_and_lower_trend_scores_lower(self):
        readings = [assess(indicator(dea=value)) for value in ('-.1', '-.02', '.02', '.1')]
        scores = [result.score for result in readings]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(len(set(scores)), 4)

    def test_long_bar_risk_is_a_graded_deduction(self):
        ordinary = assess(indicator(tail=('.02', '.04', '.1')))
        stretched = assess(indicator(tail=('.02', '.04', '.3')))
        extreme = assess(indicator(tail=('.02', '.04', '.5')))
        self.assertEqual(dict(ordinary.components)['柱长风险'], 0)
        self.assertGreater(dict(stretched.components)['柱长风险'], -2)
        self.assertLess(dict(stretched.components)['柱长风险'], 0)
        self.assertEqual(dict(extreme.components)['柱长风险'], -2)

    def test_macd_price_scale_does_not_change_the_result(self):
        original = indicator()
        for factor in (D('.00000001'), D(1), D(100000000)):
            scaled = replace(original, dif=original.dif*factor, dea=original.dea*factor, macd=original.macd*factor, macd_history=tuple(value*factor for value in original.macd_history))
            self.assertEqual(assess(scaled), assess(original))

    def test_missing_or_inconsistent_macd_history_does_not_invent_a_rating(self):
        self.assertEqual(assess(replace(indicator(), macd_history=())).label, '--')
        self.assertEqual(assess(replace(indicator(), macd=D('.3'))).label, '--')
        self.assertEqual(assess(replace(indicator(), k=D('NaN'))).label, '--')

    def test_zero_is_a_real_numeric_score_and_not_a_missing_value(self):
        result = assess(indicator(k='50', d='50', phase='重合', tail=('0', '0', '0'), baseline='0', dea='0'))
        self.assertEqual(result.score, D('0.0'))
        self.assertEqual(result.label, '0.0')
        self.assertNotIn('-0.0', result.reason)

    def test_flat_histogram_retains_a_real_nonzero_trend_without_dividing_by_zero(self):
        flat = indicator(k='50', d='50', phase='重合', tail=('0', '0', '0'), baseline='0', dea='.1')
        self.assertEqual(assess(flat).score, D('1.0'))

    def test_score_is_bounded_and_component_totals_explain_the_final_result(self):
        bearish = indicator(k='80', d='100', phase='死叉', tail=('-.02', '-.04', '-2'), baseline='.01', dea='-2')
        self.assertEqual(assess(bearish).score, -10)
        for k, d in (('10', '16'), ('30', '25'), ('55', '45'), ('85', '90'), ('90', '84')):
            for phase in ('金叉', '即将金叉', '即将死叉', '死叉', '重合'):
                for tail in (('.02', '.04', '.06'), ('.1', '.08', '.06'), ('-.02', '-.04', '-.06'), ('0', '0', '0')):
                    result = assess(indicator(k=k, d=d, phase=phase, tail=tail))
                    self.assertIsInstance(result.score, D)
                    self.assertEqual(result.score.as_tuple().exponent, -1)
                    self.assertGreaterEqual(result.score, -10)
                    self.assertLessEqual(result.score, 10)
                    self.assertEqual(sum(value for _, value in result.components), result.score)

    def test_invalid_kd_or_unknown_phase_cannot_be_a_neutral_score(self):
        for bad in (replace(indicator(), k=D(101)), replace(indicator(), d=D(-1)), replace(indicator(), kdj_signal='unknown')):
            self.assertIsNone(assess(bad).score)


class AssessmentDisplayTests(unittest.TestCase):
    def test_stale_or_failed_quotes_and_indicators_display_pending(self):
        original = row_with()
        for row in (replace(original, stale=True), replace(original, error='连接失败'), replace(original, indicators_stale=True), replace(original, indicator_error='日线失败'), replace(original, quote=None)):
            with self.subTest(row=row):
                self.assertEqual(assess_row(row).label, '待更新')
                self.assertIsNone(assess_row(row).score)
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
        self.assertEqual(headers[headers.index('MACD(日)') + 1], '技术分')
        for columns in (80, 100, 180):
            output = render([row], 10, columns=columns)
            self.assertIn(assess_row(row).label, output)
            self.assertNotIn('性价比高', output)
            self.assertIn('半导', output)
            self.assertTrue(all(width(line) <= columns for line in output.splitlines()))
        provisional = row_with(indicator(provisional=True))
        self.assertEqual(indicator_cells(provisional)['rating'].plain, assess_row(provisional).label + '(预)')

    def test_zero_intraday_score_retains_the_provisional_marker(self):
        zero = indicator(k='50', d='50', phase='重合', tail=('0', '0', '0'), baseline='0', dea='0', provisional=True)
        self.assertEqual(indicator_cells(row_with(zero))['rating'].plain, '0.0(预)')
