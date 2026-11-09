"""Explainable -10..+10 technical score; not a valuation or backtested strategy."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D, ROUND_HALF_UP

from .models import DailyIndicators, Row


KDJ_PHASES = {"金叉", "即将金叉", "即将死叉", "死叉", "重合"}
MACD_PHASES = {"红柱", "即将红柱", "即将绿柱", "绿柱", "零轴"}
KDJ_GAP_SCALE = D(10)
LONG_BAR_START = D(2)
LONG_BAR_FULL = D(4)
TENTH = D("0.1")


def clip(value: D, lower: D = D(-1), upper: D = D(1)) -> D:
    return max(lower, min(upper, value))


def rounded(value: D) -> D:
    result = value.quantize(TENTH, rounding=ROUND_HALF_UP)
    return D("0.0") if result == 0 else result


@dataclass(frozen=True)
class Assessment:
    score: D | None
    reason: str
    status: str = "--"
    components: tuple[tuple[str, D], ...] = ()

    @property
    def label(self) -> str:
        if self.score is None:
            return self.status
        return f"{self.score:+.1f}" if self.score != 0 else "0.0"


def assess(indicator: DailyIndicators) -> Assessment:
    history = indicator.macd_history
    if len(history) < 3:
        return Assessment(None, "缺少 MACD 柱长历史")
    numbers = (indicator.k, indicator.d, indicator.j, indicator.dif, indicator.dea, indicator.macd, *history)
    if not all(value.is_finite() for value in numbers) or history[-1] != indicator.macd:
        return Assessment(None, "指标数值或柱长历史不完整")
    if not (0 <= indicator.k <= 100 and 0 <= indicator.d <= 100):
        return Assessment(None, "K/D 超出有效范围")
    macd_signal = indicator.macd_signal or indicator.macd_color
    if indicator.kdj_signal not in KDJ_PHASES or macd_signal not in MACD_PHASES:
        return Assessment(None, "指标阶段无效")

    first, previous, current = history[-3:]
    # Continuous distances replace fixed points for phase labels. Crossing zero
    # changes direction smoothly; the display's warning labels do not add a bonus.
    kdj_strength = clip((indicator.k - indicator.d) / KDJ_GAP_SCALE)
    level = (indicator.k + indicator.d) / 2
    low_support = clip((50 - level) / 30, D(0)) * max(D(0), kdj_strength)
    heat = clip(max((indicator.k - 70) / 30, (indicator.d - 70) / 30,
                    (indicator.j - 80) / 40), D(0))
    # Low readings alone do not reward a falling K below D. High K/D/J readings
    # reduce the position contribution gradually instead of capping the total.
    position_points = 2 * (low_support - heat)

    window = history[-20:]
    baseline = sum(abs(value) for value in window) / len(window)
    change = (2 * (current - previous) + (previous - first)) / 3
    trend = (indicator.dif + indicator.dea) / 2
    if baseline > 0:
        bar_points = 2 * clip(current / (2 * baseline))
        momentum_points = 2 * clip(change / baseline)
        trend_points = clip(trend / (2 * baseline))
        stretch = clip((abs(current) / baseline - LONG_BAR_START)
                       / (LONG_BAR_FULL - LONG_BAR_START), D(0))
    else:
        # A genuinely flat histogram is valid. Avoid a price-dependent epsilon
        # so rescaling all prices cannot change the score.
        bar_points = momentum_points = stretch = D(0)
        trend_points = D(1) if trend > 0 else D(-1) if trend < 0 else D(0)

    raw_parts = [
        ("KDJ交叉", 3 * kdj_strength),
        ("KDJ位置", position_points),
        ("MACD柱强", bar_points),
        ("MACD变化", momentum_points),
        ("零轴趋势", trend_points),
        ("柱长风险", -2 * stretch),
    ]
    # Round the displayed components first so their visible sum is exactly the
    # displayed score. Provisional/confirmed status never changes the formula.
    parts = [(name, rounded(points)) for name, points in raw_parts]
    score = sum((points for _, points in parts), D("0.0"))
    bounded = clip(score, D(-10), D(10))
    if bounded != score:
        parts.append(("总分范围限制", bounded - score))
    reason = "；".join(f"{name}{points:+.1f}" for name, points in parts)
    return Assessment(rounded(bounded), reason, components=tuple(parts))


def assess_row(row: Row) -> Assessment:
    if row.indicators is None:
        return Assessment(None, "暂无日线指标")
    if row.quote is None or row.error or row.stale or row.indicator_error or row.indicators_stale:
        return Assessment(None, "报价或指标不可用/更新失败", "待更新")
    try:
        quote_day = date.fromisoformat(row.quote.market_time[:10])
        indicator_day = date.fromisoformat(row.indicators.as_of)
    except ValueError:
        return Assessment(None, "无法核对报价与指标日期", "待更新")
    if quote_day != indicator_day:
        return Assessment(None, "报价与日线指标日期不一致", "待更新")
    return assess(row.indicators)
