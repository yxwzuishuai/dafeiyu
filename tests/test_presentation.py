import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from io import StringIO

from rich.console import Console

from dafeiyu.indicators import calculate_daily
from dafeiyu.models import Asset, DailyBar, DailyIndicators, MarketTurnover, Quote, Row
from dafeiyu.presentation import SignalChanges, kdj_label, macd_label
from dafeiyu.render import market_summary, render, render_view


def sample_row(*, provisional=True):
    ind = DailyIndicators('2026-11-09', D(30), D(25), D(40), D('.13'), D('.1'), D('.06'), '金叉', provisional, '红柱', tuple([D('.1')]*17+[D('.02'),D('.04'),D('.06')]), '2026-11-04', 4)
    quote = Quote('stock','sh588710','科创半导体设备ETF华泰柏瑞',D('.725'),D('-.008'),D('-1.09'),D('.758'),D('.695'),'CNY','2026-11-09 13:16:00','腾讯')
    return Row(Asset('stock','sh588710'), quote, indicators=ind)


class LabelTests(unittest.TestCase):
    def test_macd_labels_distinguish_magnitude_from_signed_direction(self):
        row = sample_row()
        for tail, expected in ((('.02','.04','.06'),'红柱放大'), (('.1','.08','.06'),'红柱缩短'), (('-.06','-.04','-.02'),'绿柱缩短'), (('-.02','-.04','-.06'),'绿柱放大'), (('.02','.02','.02'),'红柱持平'), (('-.02','-.01','.01'),'新转红柱')):
            values = tuple(D(value) for value in tail)
            ind = replace(row.indicators, macd=values[-1], macd_history=values, macd_signal='红柱' if values[-1]>0 else '绿柱')
            self.assertEqual(macd_label(ind), expected)

    def test_upcoming_labels_keep_the_current_bar_shape_visible(self):
        ind = replace(sample_row().indicators, macd=D('.02'), macd_history=(D('.06'),D('.04'),D('.02')), macd_signal='即将绿柱')
        self.assertEqual(macd_label(ind), '红柱缩短·即将绿柱')

    def test_kdj_labels_distinguish_new_crosses_and_persistent_phases(self):
        ind = sample_row().indicators
        self.assertEqual(kdj_label(ind), '金叉第4日')
        self.assertEqual(kdj_label(replace(ind,kdj_phase_days=1)), '刚金叉')
        self.assertEqual(kdj_label(replace(ind,kdj_signal='即将死叉')), '即将死叉')
        self.assertEqual(kdj_label(replace(ind,kdj_phase_days=None)), '金叉阶段')

    def test_cross_age_counts_daily_bars_instead_of_calendar_days(self):
        bars = [DailyBar(f'2025-01-{day:02d}',D(11),D(9),D(10)) for day in range(1,31)]
        bars += [DailyBar('2026-11-06',D(11),D(9),D(11)), DailyBar('2026-11-09',D(11),D(9),D('10.9'))]
        # Keep enough history while leaving the last two bars on Friday and Monday.
        bars = [DailyBar('2024-12-30',D(11),D(9),D(10)), DailyBar('2024-12-31',D(11),D(9),D(10)), *bars]
        result = calculate_daily(bars)
        self.assertEqual(result.kdj_cross_day,'2026-11-06')
        self.assertEqual(result.kdj_phase_days,2)
        self.assertEqual(kdj_label(result),'金叉第2日')


class SummaryAndDetailsTests(unittest.TestCase):
    def test_top_summary_contains_percent_change_and_source_time_once(self):
        market = MarketTurnover(D(100000000),D(200000000),datetime.fromisoformat('2026-11-09T13:15:00+08:00'),'测试',D(400000000),'2026-11-06','同期')
        summary = market_summary(market).plain
        self.assertIn('3.00亿元',summary)
        self.assertIn('缩量 1.00亿元',summary)
        self.assertIn('-25.00%',summary)
        self.assertIn('11-09 13:15',summary)
        text = render([sample_row(),sample_row()],10,market=market,columns=160)
        self.assertEqual(text.count('3.00亿元'),1)
        self.assertNotIn('大A量能(亿)',text)

    def test_zero_comparison_base_does_not_create_an_infinite_percentage(self):
        market = MarketTurnover(D(100000000),D(200000000),datetime.fromisoformat('2026-11-09T13:15:00+08:00'),'测试',D(0),'2026-11-06','同期')
        self.assertIn('幅度--',market_summary(market).plain)

    def test_uniform_provisional_data_is_marked_once_in_the_title(self):
        text = render([sample_row(),sample_row()],10,columns=180)
        self.assertEqual(text.count('盘中预估'),1)
        self.assertNotIn('(预)',text)
        self.assertIn('技术分',text)
        self.assertNotIn('性价比',text)

    def test_mixed_or_stale_rows_keep_their_own_status_flags(self):
        row = sample_row()
        text = render([row,replace(row,indicators=replace(row.indicators,provisional=False),indicators_stale=True)],10,columns=180)
        self.assertIn('部分盘中预估',text)
        self.assertIn('(预)',text)
        self.assertIn('(旧)',text)

    def test_score_details_are_opt_in_and_explain_decimal_components(self):
        row = sample_row()
        self.assertNotIn('技术分明细',render([row],10,columns=180))
        text = render([row],10,columns=180,score_details=True)
        self.assertIn('技术分明细',text)
        self.assertIn('KDJ交叉+1.5',text)
        self.assertIn('MACD柱强',text)
        self.assertNotIn('盘中正分上限',text)
        self.assertIn('K/D/J',text)


class ChangeHighlightTests(unittest.TestCase):
    def setUp(self):
        self.time = 0.0
        self.changes = SignalChanges(clock=lambda:self.time)
        self.row = sample_row(provisional=False)

    def test_first_frame_is_not_highlighted_and_real_changes_expire(self):
        self.changes.observe([self.row])
        self.assertFalse(self.changes.active())
        newer = replace(self.row,indicators=replace(self.row.indicators,k=D(25),d=D(30),j=D(15),kdj_signal='死叉',kdj_cross_day='2026-11-09',kdj_phase_days=1))
        self.changes.observe([newer])
        self.assertIn((newer.asset.key,'kdj'),self.changes.active())
        self.assertIn((newer.asset.key,'rating'),self.changes.active())
        self.time = 3.1
        self.assertFalse(self.changes.active())
        self.assertTrue(self.changes.expire())
        self.assertFalse(self.changes.expire())

    def test_stale_data_does_not_create_or_keep_a_signal_highlight(self):
        self.changes.observe([self.row])
        stale = replace(self.row,stale=True,indicators=replace(self.row.indicators,kdj_signal='死叉'))
        self.changes.observe([stale])
        self.assertFalse(self.changes.active())
        self.changes.observe([self.row])
        self.assertFalse(self.changes.active())

    def test_sub_tenth_score_movement_does_not_flash_an_unchanged_display(self):
        self.changes.observe([self.row])
        ind = self.row.indicators
        tiny_change = replace(self.row, indicators=replace(ind, k=ind.k+D('.001'), j=ind.j+D('.003')))
        self.changes.observe([tiny_change])
        self.assertFalse(self.changes.active())

    def test_change_emphasis_is_applied_to_the_cell_then_removed(self):
        console = Console(file=StringIO(),width=180,force_terminal=True,color_system='standard')
        highlighted = {(self.row.asset.key,'rating')}
        styled = list(console.render(render_view([self.row],10,columns=180,highlighted=highlighted),console.options))
        ordinary = list(console.render(render_view([self.row],10,columns=180),console.options))
        self.assertTrue(any(segment.style and segment.style.reverse for segment in styled))
        self.assertFalse(any(segment.style and segment.style.reverse for segment in ordinary))
