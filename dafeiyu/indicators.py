"""Daily KDJ(9,3,3) and MACD(12,26,9), without third-party numeric libraries."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from .models import Asset, DailyBar, DailyIndicators


CHINA_TIME = timezone(timedelta(hours=8))
MIN_DAILY_BARS = 34
# Project-specific warning defaults; these are heuristics, not guaranteed crossings.
WARNING_HORIZON = D(2)
KDJ_WARNING_GAP = D(5)


def phase_signal(
    values: list[D], *, positive: str, negative: str, neutral: str,
    to_positive: str, to_negative: str, max_gap: D | None = None,
) -> str:
    """Classify the present phase, with a two-daily-bar convergence warning."""
    current = values[-1]
    state = positive if current > 0 else negative if current < 0 else neutral
    if len(values) < 3 or (max_gap is not None and abs(current) > max_gap):
        return state
    first, previous, current = values[-3:]
    toward_positive = first < previous < current <= 0
    toward_negative = first > previous > current >= 0
    if not (toward_positive or toward_negative):
        return state
    # Same-direction changes make this the mean speed over two daily bars.
    speed = abs(current - first) / 2
    if abs(current) / speed <= WARNING_HORIZON:
        return to_positive if toward_positive else to_negative
    return state


def supports_daily(asset: Asset) -> bool:
    return asset.kind == "stock" and asset.code.startswith(("sh", "sz", "bj"))


def calculate_daily(bars: list[DailyBar], *, now: datetime | None = None) -> DailyIndicators:
    if len(bars) < MIN_DAILY_BARS:
        raise ValueError(f"日线不足 {MIN_DAILY_BARS} 根，暂不生成指标")
    for index, bar in enumerate(bars):
        if (not all(value.is_finite() and value > 0 for value in (bar.high, bar.low, bar.close))
                or not bar.low <= bar.close <= bar.high):
            raise ValueError("日线价格不合法")
        if index and bars[index - 1].day >= bar.day:
            raise ValueError("日线日期必须递增且不能重复")

    k = d = D(50)
    ema12 = ema26 = bars[0].close
    dea = D(0)
    kdj_gaps, macd_values = [], []
    last_cross_index = None
    for index, bar in enumerate(bars):
        window = bars[max(0, index - 8):index + 1]
        high, low = max(item.high for item in window), min(item.low for item in window)
        rsv = (bar.close - low) / (high - low) * 100 if high != low else D(50)
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        ema12 += (bar.close - ema12) * 2 / 13
        ema26 += (bar.close - ema26) * 2 / 27
        dif = ema12 - ema26
        dea += (dif - dea) * 2 / 10
        kdj_gaps.append(k - d)
        macd_values.append(2 * (dif - dea))
        # Record crosses only after both nine-bar lookback windows are available.
        if index >= 9:
            previous_gap, current_gap = kdj_gaps[-2:]
            if (previous_gap <= 0 < current_gap) or (previous_gap >= 0 > current_gap):
                last_cross_index = index

    # Stages use actual daily history, never consecutive ten-second polls.
    signal = phase_signal(
        kdj_gaps, positive="金叉", negative="死叉", neutral="重合",
        to_positive="即将金叉", to_negative="即将死叉", max_gap=KDJ_WARNING_GAP,
    )
    macd_signal = phase_signal(
        macd_values, positive="红柱", negative="绿柱", neutral="零轴",
        to_positive="即将红柱", to_negative="即将绿柱",
    )
    now = now.astimezone(CHINA_TIME) if now is not None else datetime.now(CHINA_TIME)
    day = datetime.strptime(bars[-1].day, "%Y-%m-%d").date()
    if day > now.date():
        raise ValueError("日线日期晚于当前日期")
    provisional = day == now.date() and now.weekday() < 5 and (now.hour, now.minute) < (15, 0)
    return DailyIndicators(
        bars[-1].day, k, d, 3 * k - 2 * d, dif, dea, macd_values[-1], signal,
        provisional, macd_signal, tuple(macd_values[-20:]),
        kdj_cross_day=bars[last_cross_index].day if last_cross_index is not None else "",
        kdj_phase_days=len(bars) - last_cross_index if last_cross_index is not None else None,
    )
