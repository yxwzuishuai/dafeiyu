"""Transparent technical-signal buckets; not a valuation or a backtested strategy."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D

from .models import DailyIndicators, Row


LOW_MID_LIMIT = D(50)
TRIAL_LIMIT = D(60)
LONG_BAR_MULTIPLE = D(2)


@dataclass(frozen=True)
class Assessment:
    label: str
    reason: str


def assess(indicator: DailyIndicators) -> Assessment:
    history = indicator.macd_history
    if len(history) < 3:
        return Assessment("--", "缺少 MACD 柱长历史")
    numbers = (indicator.k, indicator.d, indicator.j, indicator.dif, indicator.dea, indicator.macd, *history)
    if not all(value.is_finite() for value in numbers) or history[-1] != indicator.macd:
        return Assessment("--", "指标数值或柱长历史不完整")

    first, previous, current = history[-3:]
    strengthening = first < previous < current
    weakening = first > previous > current
    kdj_up = indicator.kdj_signal in {"金叉", "即将金叉"}
    kdj_down = indicator.kdj_signal in {"死叉", "即将死叉"}
    macd_down = (current < 0 and not strengthening) or (current > 0 and weakening) or indicator.macd_signal == "即将绿柱"

    # Risk combinations take precedence over oversold or a single positive signal.
    if kdj_down and macd_down:
        return Assessment("不建议买", "KDJ 走弱，且 MACD 红柱衰减或绿柱未连续改善")
    if indicator.k >= 80 or indicator.d >= 80 or indicator.j >= 100:
        return Assessment("性价比低", "KDJ 位置偏高，组合规则不追高")

    # A growing green bar is more negative; shrinking green bars are improving.
    # Compare against this asset's own 20-bar baseline, not a fixed price amount.
    baseline = sum(abs(value) for value in history[-20:]) / len(history[-20:])
    long_bar = baseline > 0 and abs(current) > LONG_BAR_MULTIPLE * baseline
    low_mid = indicator.k <= LOW_MID_LIMIT and indicator.d <= LOW_MID_LIMIT and 0 <= indicator.j <= 80
    confirmed_up = indicator.kdj_signal == "金叉" and current > 0 and strengthening
    trend_up = indicator.dif >= 0 and indicator.dea >= 0
    if low_mid and confirmed_up and trend_up and not long_bar:
        if indicator.provisional:
            return Assessment("小仓位", "组合向好，但本根日线尚未收盘确认")
        return Assessment("性价比高", "低中位金叉、红柱连续增强且 DIF/DEA 在零轴上，柱长未过度扩张")

    improving = strengthening and current != 0
    not_chasing = indicator.k < TRIAL_LIMIT and indicator.d < TRIAL_LIMIT and indicator.j < 100
    if kdj_up and improving and not_chasing:
        return Assessment("小仓位", "低中位 KDJ 改善，MACD 新转红、红柱增强或绿柱连续缩短，尚未满足全部确认条件")
    return Assessment("性价比低", "信号尚未形成完整组合，或位置与动能不匹配")


def assess_row(row: Row) -> Assessment:
    if row.indicators is None:
        return Assessment("--", "暂无日线指标")
    if row.quote is None or row.error or row.stale or row.indicator_error or row.indicators_stale:
        return Assessment("待更新", "报价或指标不可用/更新失败")
    try:
        quote_day = date.fromisoformat(row.quote.market_time[:10])
        indicator_day = date.fromisoformat(row.indicators.as_of)
    except ValueError:
        return Assessment("待更新", "无法核对报价与指标日期")
    if quote_day != indicator_day:
        return Assessment("待更新", "报价与日线指标日期不一致")
    return assess(row.indicators)
