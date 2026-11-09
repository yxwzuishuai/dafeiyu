"""One shared SSE/SZSE composite-index turnover snapshot, in CNY yuan."""

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .models import MarketTurnover, number


MARKET_TIMEZONE = timezone(timedelta(hours=8))
MAX_TIME_SKEW = timedelta(minutes=2)
MARKET_CODES = {("1", "000001"): "sh000001", ("0", "399106"): "sz399106"}


class MarketDataError(ValueError):
    pass


def combine_market(parts: dict[str, tuple[Decimal, datetime]], source: str) -> MarketTurnover:
    if set(parts) != {"sh000001", "sz399106"}:
        raise MarketDataError("缺少沪市或深市成交额，不能作为两市合计")
    sh_amount, sh_time = parts["sh000001"]
    sz_amount, sz_time = parts["sz399106"]
    if sh_time.date() != sz_time.date():
        raise MarketDataError("沪深成交额日期不一致")
    if abs(sh_time - sz_time) > MAX_TIME_SKEW:
        raise MarketDataError("沪深成交额更新时间相差超过 2 分钟")
    return MarketTurnover(sh_amount, sz_amount, min(sh_time, sz_time), source)


def parse_market_eastmoney(text: str) -> MarketTurnover:
    try:
        payload = json.loads(text, parse_float=Decimal)
        if payload.get("rc") != 0:
            raise MarketDataError("东方财富市场接口返回失败")
        records = payload["data"]["diff"]
        if not isinstance(records, list):
            raise MarketDataError("东方财富市场接口未返回指数列表")
        parts = {}
        for record in records:
            code = MARKET_CODES.get((str(record.get("f13")), str(record.get("f12"))))
            if code is None:
                continue
            if code in parts:
                raise MarketDataError("市场指数重复，不能重复计入成交额")
            amount, timestamp = number(record.get("f6")), number(record.get("f124"))
            if amount is None or amount < 0 or timestamp is None or timestamp <= 0 or timestamp != timestamp.to_integral_value():
                raise MarketDataError("指数成交额或时间缺失/无效")
            stamp = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(MARKET_TIMEZONE)
            parts[code] = amount, stamp
        return combine_market(parts, "东方财富")
    except MarketDataError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
        raise MarketDataError("东方财富市场数据格式异常") from None


def parse_market_sina(text: str) -> MarketTurnover:
    parts = {}
    for match in re.finditer(r'var\s+hq_str_(sh000001|sz399106)\s*=\s*"([^"\r\n]*)"\s*;', text):
        code, payload = match.groups()
        if code in parts:
            raise MarketDataError("市场指数重复，不能重复计入成交额")
        values = payload.split(",")
        if len(values) < 32:
            raise MarketDataError("新浪指数成交额字段不完整")
        amount = number(values[9])  # Full quote field 9 is CNY yuan, not shares or ten-thousand yuan.
        if amount is None or amount < 0:
            raise MarketDataError("新浪指数成交额缺失/无效")
        try:
            stamp = datetime.strptime(f"{values[30]} {values[31]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=MARKET_TIMEZONE)
        except ValueError:
            raise MarketDataError("新浪指数更新时间无效") from None
        parts[code] = amount, stamp
    return combine_market(parts, "新浪")


def comparison_cutoff(market: MarketTurnover, now: datetime | None = None) -> datetime:
    """Use a fully completed minute, including the noon break and closing auction."""
    clock = now.astimezone(MARKET_TIMEZONE) if now is not None else datetime.now(MARKET_TIMEZONE)
    stamp = market.market_time.astimezone(MARKET_TIMEZONE)
    if stamp > clock + MAX_TIME_SKEW:
        raise MarketDataError("市场快照时间晚于当前时间")
    close = stamp.replace(hour=15, minute=0, second=0, microsecond=0)
    if stamp >= close and clock >= close:
        return close
    cutoff = min(stamp.replace(second=0, microsecond=0), (clock - timedelta(minutes=1)).replace(second=0, microsecond=0))
    if cutoff.date() != stamp.date() or cutoff.strftime("%H%M") < "0930":
        raise MarketDataError("尚无完整交易分钟可供比较")
    if "1130" < cutoff.strftime("%H%M") < "1300":
        cutoff = cutoff.replace(hour=11, minute=30)
    return cutoff


def parse_market_minutes(text: str, code: str) -> dict[str, dict[str, Decimal]]:
    try:
        payload = json.loads(text)
        if payload.get("code") != 0:
            raise MarketDataError("腾讯五日分时返回失败")
        records = payload["data"][code]["data"]
        if not isinstance(records, list) or not records:
            raise MarketDataError("缺少五日分时数据")
        days = {}
        for record in records:
            day = datetime.strptime(str(record["date"]), "%Y%m%d").strftime("%Y-%m-%d")
            if day in days:
                raise MarketDataError("分时交易日重复")
            lines = record["data"]
            if not isinstance(lines, list) or not lines:
                raise MarketDataError("分时交易日缺少数据")
            points = {}
            for line in lines:
                fields = line.split()
                if len(fields) < 4 or not re.fullmatch(r"\d{4}", fields[0]):
                    raise MarketDataError("分时字段不完整")
                minute = datetime.strptime(fields[0], "%H%M").strftime("%H%M")
                if not "0925" <= minute <= "1500" or "1130" < minute < "1300":
                    raise MarketDataError("分时交易时间无效")
                amount = number(fields[3])  # Cumulative yuan, never sum it as minute turnover.
                if amount is None or amount < 0 or minute in points:
                    raise MarketDataError("分时累计成交额无效或分钟重复")
                points[minute] = amount
            ordered = [points[minute] for minute in sorted(points)]
            if any(left > right for left, right in zip(ordered, ordered[1:])):
                raise MarketDataError("分时累计成交额发生回退")
            days[day] = points
        return days
    except MarketDataError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise MarketDataError("腾讯五日分时格式异常或代码不匹配") from None


def parse_market_days(text: str, code: str) -> dict[str, Decimal]:
    try:
        payload = json.loads(text)
        data = payload["data"]
        if payload.get("rc") != 0 or str(data["code"]) != code[2:]:
            raise MarketDataError("日线成交额代码不匹配或返回失败")
        if str(data["market"]) != ("1" if code.startswith("sh") else "0"):
            raise MarketDataError("日线成交额市场不匹配")
        records = data["klines"]
        if not isinstance(records, list) or not records:
            raise MarketDataError("缺少日线成交额")
        days = {}
        for line in records:
            fields = line.split(",")
            if len(fields) < 7:
                raise MarketDataError("日线成交额字段不完整")
            day = datetime.strptime(fields[0], "%Y-%m-%d").strftime("%Y-%m-%d")
            amount = number(fields[6])
            if amount is None or amount < 0 or day in days:
                raise MarketDataError("日线成交额无效或日期重复")
            days[day] = amount
        return days
    except MarketDataError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise MarketDataError("日线成交额格式异常") from None


def previous_market_day(day: str, sh_days: dict, sz_days: dict) -> str:
    if day not in sh_days or day not in sz_days:
        raise MarketDataError("缺少当前交易日的沪深成交额")
    sh_previous = max((value for value in sh_days if value < day), default=None)
    sz_previous = max((value for value in sz_days if value < day), default=None)
    if sh_previous is None or sh_previous != sz_previous:
        raise MarketDataError("上一交易日缺失或沪深日期不一致")
    return sh_previous


def compare_market_minutes(market: MarketTurnover, histories: dict[str, dict], cutoff: datetime) -> MarketTurnover:
    sh, sz = histories["sh000001"], histories["sz399106"]
    day = cutoff.strftime("%Y-%m-%d")
    previous = previous_market_day(day, sh, sz)
    minutes = [minute for minute in sh[day].keys() & sz[day].keys() if minute <= cutoff.strftime("%H%M")]
    if not minutes:
        raise MarketDataError("缺少完整分钟成交额")
    minute = max(minutes)
    stamp = datetime.strptime(day + minute, "%Y-%m-%d%H%M").replace(tzinfo=MARKET_TIMEZONE)
    if cutoff - stamp > MAX_TIME_SKEW:
        raise MarketDataError("完整分钟数据延迟超过 2 分钟")
    if minute not in sh[previous] or minute not in sz[previous]:
        raise MarketDataError("上一交易日缺少对应分钟，不能作同期比较")
    return replace(
        market, shanghai_amount=sh[day][minute], shenzhen_amount=sz[day][minute], market_time=stamp,
        source="腾讯分时", previous_amount=sh[previous][minute] + sz[previous][minute],
        previous_day=previous, comparison_kind="同期", comparison_error="",
    )


def compare_market_days(market: MarketTurnover, histories: dict[str, dict]) -> MarketTurnover:
    sh, sz = histories["sh000001"], histories["sz399106"]
    day = market.market_time.strftime("%Y-%m-%d")
    previous = previous_market_day(day, sh, sz)
    return replace(
        market, shanghai_amount=sh[day], shenzhen_amount=sz[day], source="东方财富日线",
        previous_amount=sh[previous] + sz[previous], previous_day=previous,
        comparison_kind="全天", comparison_error="",
    )
