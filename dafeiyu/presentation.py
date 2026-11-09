"""Signal labels and short-lived change tracking, independent of network requests."""

import time

from .assessment import assess_row
from .models import DailyIndicators, Key, Row


def kdj_label(indicator: DailyIndicators) -> str:
    phase = indicator.kdj_signal
    if phase not in {"金叉", "死叉"}:
        return phase
    if indicator.kdj_phase_days == 1:
        return "刚" + phase
    if indicator.kdj_phase_days is not None:
        return f"{phase}第{indicator.kdj_phase_days}日"
    return phase + "阶段"


def macd_label(indicator: DailyIndicators) -> str:
    current = indicator.macd
    history = indicator.macd_history
    previous = history[-2] if len(history) >= 2 and history[-1] == current else None
    if current == 0:
        label = "零轴"
    elif previous is None:
        label = indicator.macd_color
    elif current > 0 >= previous:
        label = "新转红柱"
    elif current < 0 <= previous:
        label = "新转绿柱"
    else:
        shape = "放大" if abs(current) > abs(previous) else "缩短" if abs(current) < abs(previous) else "持平"
        label = indicator.macd_color + shape
    if indicator.macd_signal.startswith("即将"):
        label += "·" + indicator.macd_signal
    return label


class SignalChanges:
    def __init__(self, *, duration: float = 3.0, clock=time.monotonic):
        self.duration = duration
        self.clock = clock
        self._previous: dict[Key, dict[str, object]] = {}
        self._expires: dict[tuple[Key, str], float] = {}

    def observe(self, rows: list[Row]) -> None:
        keys = {row.asset.key for row in rows}
        self._previous = {key: value for key, value in self._previous.items() if key in keys}
        self._expires = {key: value for key, value in self._expires.items() if key[0] in keys}
        for row in rows:
            result = assess_row(row)
            if row.indicators is None or result.score is None:
                self._expires = {key: value for key, value in self._expires.items() if key[0] != row.asset.key}
                continue
            indicator = row.indicators
            current = {
                "kdj": (indicator.kdj_signal, indicator.kdj_cross_day),
                "macd": macd_label(indicator),
                "rating": result.score,
            }
            previous = self._previous.get(row.asset.key)
            if previous is not None:
                for column, value in current.items():
                    if previous[column] != value:
                        self._expires[row.asset.key, column] = self.clock() + self.duration
            self._previous[row.asset.key] = current

    def active(self) -> set[tuple[Key, str]]:
        now = self.clock()
        return {key for key, deadline in self._expires.items() if deadline > now}

    def expire(self) -> bool:
        now = self.clock()
        expired = [key for key, deadline in self._expires.items() if deadline <= now]
        for key in expired:
            del self._expires[key]
        return bool(expired)
