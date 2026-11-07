"""Daily KDJ(9,3,3) and MACD(12,26,9), without third-party numeric libraries."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from .models import Asset, DailyBar, DailyIndicators


CHINA_TIME = timezone(timedelta(hours=8))
MIN_DAILY_BARS = 34


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
    for index, bar in enumerate(bars):
        window = bars[max(0, index - 8):index + 1]
        high, low = max(item.high for item in window), min(item.low for item in window)
        rsv = (bar.close - low) / (high - low) * 100 if high != low else D(50)
        previous_k, previous_d = k, d
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        ema12 += (bar.close - ema12) * 2 / 13
        ema26 += (bar.close - ema26) * 2 / 27
        dif = ema12 - ema26
        dea += (dif - dea) * 2 / 10

    # Crosses compare consecutive daily bars, never consecutive ten-second polls.
    signal = "金叉" if previous_k <= previous_d and k > d else "死叉" if previous_k >= previous_d and k < d else "无新交叉"
    now = now.astimezone(CHINA_TIME) if now is not None else datetime.now(CHINA_TIME)
    day = datetime.strptime(bars[-1].day, "%Y-%m-%d").date()
    if day > now.date():
        raise ValueError("日线日期晚于当前日期")
    provisional = day == now.date() and now.weekday() < 5 and (now.hour, now.minute) < (15, 0)
    return DailyIndicators(bars[-1].day, k, d, 3 * k - 2 * d, dif, dea, 2 * (dif - dea), signal, provisional)
