import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .market import (
    MarketDataError, compare_market_days, compare_market_minutes, comparison_cutoff,
    parse_market_days, parse_market_eastmoney, parse_market_minutes, parse_market_sina,
)
from .models import DailyBar, Key, MarketTurnover, Quote, number


class FetchError(RuntimeError):
    pass


@dataclass
class Batch:
    quotes: dict[Key, Quote] = field(default_factory=dict)
    errors: dict[Key, str] = field(default_factory=dict)


class HttpClient:
    def __init__(self, timeout: float = 6, proxy: str | None = None):
        self.timeout = timeout
        self.proxy = proxy

    def get(self, url: str, *, encoding: str = "utf-8", referer: str = "") -> str:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Dafeiyu/0.1)", "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        handlers = [ProxyHandler({"http": self.proxy, "https": self.proxy})] if self.proxy else []
        # A separate opener per request avoids sharing mutable connections across threads.
        opener = build_opener(*handlers)
        try:
            with opener.open(Request(url, headers=headers), timeout=self.timeout) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise FetchError("行情响应异常过大")
                return body.decode(encoding)
        except HTTPError as error:
            raise FetchError(f"行情服务返回 HTTP {error.code}") from None
        except URLError as error:
            raise FetchError(f"网络请求失败：{error.reason}") from None
        except (OSError, UnicodeError) as error:
            raise FetchError(f"请求失败：{error}") from None


def market_time(raw: object) -> str:
    value = str(raw or "").strip()
    for pattern in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return value or "--"


def parse_tencent(text: str, codes: list[str]) -> Batch:
    batch = Batch()
    requested = set(codes)
    for match in re.finditer(r'v_([A-Za-z0-9._-]+)\s*=\s*"([^"\r\n]*)"\s*;', text):
        code, payload = match.groups()
        if code not in requested:
            continue
        key = "stock", code
        values = payload.split("~")
        if len(values) < 35:
            batch.errors[key] = "代码不存在或行情字段不完整"
            continue
        price = number(values[3])
        if price is None or price <= 0:
            batch.errors[key] = "暂无有效报价（可能未上市或停牌）"
            continue
        currency = "USD" if code.startswith("us") else "HKD" if code.startswith("hk") else "CNY"
        batch.quotes[key] = Quote(
            "stock", code, values[1], price,
            number(values[31]), number(values[32]), number(values[33]), number(values[34]),
            currency, market_time(values[30]), "腾讯",
        )
    for code in codes:
        if ("stock", code) not in batch.quotes:
            batch.errors.setdefault(("stock", code), "行情源未返回该代码")
    return batch


def parse_sina(text: str, codes: list[str]) -> Batch:
    batch = Batch()
    for match in re.finditer(r'var\s+hq_str_([A-Za-z0-9]+)\s*=\s*"([^"\r\n]*)"\s*;', text):
        code, payload = match.groups()
        if code not in codes:
            continue
        values = payload.split(",")
        key = "stock", code
        if len(values) < 32:
            batch.errors[key] = "新浪行情字段不完整"
            continue
        current, previous = number(values[3]), number(values[2])
        if current is None or current <= 0:
            batch.errors[key] = "暂无有效报价"
            continue
        change = current - previous if previous is not None else None
        percentage = change / previous * 100 if change is not None and previous and previous > 0 else None
        batch.quotes[key] = Quote(
            "stock", code, values[0], current, change, percentage,
            number(values[4]), number(values[5]), "CNY",
            market_time(f"{values[30]} {values[31]}"), "新浪",
        )
    for code in codes:
        if ("stock", code) not in batch.quotes:
            batch.errors.setdefault(("stock", code), "新浪行情源未返回该代码")
    return batch


def parse_funds(text: str, codes: list[str]) -> Batch:
    try:
        payload = json.loads(text, parse_float=Decimal)
    except (json.JSONDecodeError, TypeError):
        raise FetchError("基金接口未返回有效 JSON 数据") from None
    records = payload.get("Datas") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise FetchError("基金接口未返回净值列表")
    batch = Batch()
    for record in records:
        if not isinstance(record, dict):
            continue
        code = str(record.get("FCODE", ""))
        if code not in codes:
            continue
        key = "fund", code
        estimated = number(record.get("GSZ"))
        use_estimate = estimated is not None and estimated > 0
        price = estimated if use_estimate else number(record.get("NAV"))
        if price is None or price <= 0:
            batch.errors[key] = "该基金暂未公布有效净值"
            continue
        percentage = number(record.get("GSZZL") if use_estimate else record.get("NAVCHGRT"))
        # Dividend-adjusted fund returns do not always equal a simple NAV difference.
        batch.quotes[key] = Quote(
            "fund", code, str(record.get("SHORTNAME") or code), price,
            None, percentage, None, None, "CNY",
            market_time(record.get("GZTIME") if use_estimate else record.get("PDATE")),
            "天天基金", "估值" if use_estimate else "净值",
        )
    for code in codes:
        if ("fund", code) not in batch.quotes:
            batch.errors.setdefault(("fund", code), "基金接口未返回该代码")
    return batch


def parse_yahoo(text: str, code: str) -> Quote:
    try:
        payload = json.loads(text, parse_float=Decimal)
        chart = payload["chart"]
        if chart.get("error"):
            raise FetchError("雅虎行情源未返回该加密货币")
        result = chart["result"][0]
        meta = result["meta"]
        price = number(meta.get("regularMarketPrice"))
        if price is None or price <= 0:
            raise FetchError("加密货币暂无有效报价")
        returned_symbol = str(meta.get("symbol", "")).upper()
        if returned_symbol != code:
            raise FetchError("加密货币返回代码与请求不一致")
        change = number(meta.get("regularMarketChange"))
        if change is None:
            change = number(meta.get("fulldayChange"))
        percentage = number(meta.get("regularMarketChangePercent"))
        if percentage is None:
            percentage = number(meta.get("fulldayChangePercent"))
        if change is None and percentage is None:
            closes = [number(value) for value in result["indicators"]["quote"][0]["close"]]
            closes = [value for value in closes if value is not None]
            if len(closes) >= 2 and closes[-2] > 0:
                change = price - closes[-2]
                percentage = change / closes[-2] * 100
        elif percentage is None and change is not None and price - change > 0:
            percentage = change / (price - change) * 100
        elif change is None and percentage is not None and percentage > -100:
            change = price - price / (1 + percentage / 100)
        timestamp = meta.get("regularMarketTime")
        quote_time = datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if timestamp else "--"
        return Quote(
            "coin", code, str(meta.get("shortName") or meta.get("longName") or code),
            price, change, percentage, number(meta.get("regularMarketDayHigh")),
            number(meta.get("regularMarketDayLow")), str(meta.get("currency") or code.rsplit("-", 1)[-1]),
            quote_time, "雅虎",
        )
    except FetchError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError):
        raise FetchError("雅虎接口返回数据缺失或格式发生变化") from None


def parse_daily_bars(records: object) -> list[DailyBar]:
    if not isinstance(records, list) or not records:
        raise FetchError("日线接口未返回历史数据")
    bars = []
    for record in records:
        fields = record.split(",") if isinstance(record, str) else record
        if not isinstance(fields, (list, tuple)) or len(fields) < 5:
            raise FetchError("日线字段缺失")
        day = str(fields[0])
        try:
            parsed_day = datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise FetchError("日线日期无效") from None
        prices = [number(value) for value in fields[1:5]]
        if any(value is None or value <= 0 for value in prices):
            raise FetchError("日线价格缺失或不合法")
        open_, close, high, low = prices
        if not low <= min(open_, close) <= max(open_, close) <= high:
            raise FetchError("日线高低价不合法")
        bars.append(DailyBar(parsed_day, high, low, close))
    bars.sort(key=lambda bar: bar.day)
    if len({bar.day for bar in bars}) != len(bars):
        raise FetchError("日线日期重复")
    return bars


def parse_tencent_daily(text: str, code: str) -> list[DailyBar]:
    try:
        payload = json.loads(text)
        if payload.get("code") != 0:
            raise FetchError("腾讯日线接口返回失败")
        # Do not silently substitute unadjusted bars for the requested qfq series.
        records = payload["data"][code]["qfqday"]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise FetchError("腾讯未返回该代码的前复权日线") from None
    return parse_daily_bars(records)


def parse_eastmoney_daily(text: str, code: str) -> list[DailyBar]:
    try:
        payload = json.loads(text)
        data = payload["data"]
        if payload.get("rc") != 0 or str(data["code"]) != code[2:]:
            raise FetchError("东方财富日线代码不匹配或接口返回失败")
        records = data["klines"]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise FetchError("东方财富未返回该代码的日线") from None
    return parse_daily_bars(records)


class Providers:
    def __init__(self, client: HttpClient, stock_source: str = "tencent"):
        self.client = client
        self.stock_source = stock_source
        self._market_comparison_key = None
        self._market_comparison_histories = {}
        self._market_comparison_error = ""
        self._market_comparison_attempt = 0.0

    def market_turnover(self, *, now: datetime | None = None) -> MarketTurnover:
        market = self._market_snapshot()
        try:
            cutoff = comparison_cutoff(market, now)
            daily = cutoff.strftime("%H%M") == "1500"
            key = (cutoff, market.total_amount if daily else None)
            retry = self._market_comparison_error and time.monotonic() - self._market_comparison_attempt >= 60
            if key != self._market_comparison_key or retry:
                self._market_comparison_key = key
                self._market_comparison_attempt = time.monotonic()
                self._market_comparison_histories = {}
                self._market_comparison_error = ""
                try:
                    def history(code):
                        if not daily:
                            body = self.client.get("https://web.ifzq.gtimg.cn/appstock/app/day/query?" + urlencode({"code": code}))
                            return code, parse_market_minutes(body, code)
                        params = urlencode({
                            "secid": f"{1 if code.startswith('sh') else 0}.{code[2:]}", "klt": "101", "fqt": "0",
                            "beg": (cutoff - timedelta(days=45)).strftime("%Y%m%d"), "end": cutoff.strftime("%Y%m%d"),
                            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
                        })
                        body = self.client.get("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + params)
                        return code, parse_market_days(body, code)
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        self._market_comparison_histories = dict(executor.map(history, ("sh000001", "sz399106")))
                except (FetchError, MarketDataError) as error:
                    self._market_comparison_error = str(error)
            if self._market_comparison_error:
                raise MarketDataError(self._market_comparison_error)
            if daily:
                return compare_market_days(market, self._market_comparison_histories)
            return compare_market_minutes(market, self._market_comparison_histories, cutoff)
        except (FetchError, MarketDataError) as error:
            self._market_comparison_error = str(error)
            return replace(market, comparison_error=str(error))

    def _market_snapshot(self) -> MarketTurnover:
        params = urlencode({
            "secids": "1.000001,0.399106", "fields": "f12,f13,f14,f6,f124",
            "fltt": "2", "invt": "2",
        })
        try:
            return parse_market_eastmoney(self.client.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get?" + params,
                referer="https://quote.eastmoney.com/",
            ))
        except (FetchError, MarketDataError) as first_error:
            try:
                return parse_market_sina(self.client.get(
                    "https://hq.sinajs.cn/list=sh000001,sz399106", encoding="gb18030",
                    referer="https://finance.sina.com.cn/",
                ))
            except (FetchError, MarketDataError) as second_error:
                raise FetchError(f"两市成交额获取失败：{first_error}；备用源：{second_error}") from None

    def daily(self, code: str) -> list[DailyBar]:
        if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", code):
            raise FetchError("日线指标暂仅支持沪深京股票和场内 ETF")
        params = urlencode({"param": f"{code},day,,,640,qfq"})
        try:
            return parse_tencent_daily(self.client.get(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + params,
                referer="https://gu.qq.com/",
            ), code)
        except FetchError as first_error:
            params = urlencode({
                "secid": f"{1 if code.startswith('sh') else 0}.{code[2:]}",
                "klt": "101", "fqt": "1", "beg": "19900101", "end": "20500101", "lmt": "640",
                "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            })
            try:
                return parse_eastmoney_daily(self.client.get(
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + params,
                    referer="https://quote.eastmoney.com/",
                ), code)
            except FetchError as second_error:
                raise FetchError(f"日线获取失败：{first_error}；备用源：{second_error}") from None

    def stocks(self, codes: list[str]) -> Batch:
        batches = []
        # Bound URL length for larger watchlists.
        for offset in range(0, len(codes), 80):
            chunk = codes[offset:offset + 80]
            try:
                if self.stock_source == "sina":
                    text = self.client.get("https://hq.sinajs.cn/list=" + ",".join(chunk), encoding="gb18030", referer="https://finance.sina.com.cn/")
                    batch = parse_sina(text, chunk)
                else:
                    text = self.client.get("https://qt.gtimg.cn/q=" + ",".join(chunk), encoding="gb18030", referer="https://finance.qq.com/")
                    batch = parse_tencent(text, chunk)
            except FetchError as error:
                batch = Batch(errors={("stock", code): str(error) for code in chunk})
            batches.append(batch)
        return Batch(
            quotes={key: value for batch in batches for key, value in batch.quotes.items()},
            errors={key: value for batch in batches for key, value in batch.errors.items()},
        )

    def funds(self, codes: list[str]) -> Batch:
        merged = Batch()
        for offset in range(0, len(codes), 50):
            chunk = codes[offset:offset + 50]
            params = urlencode({
                "Fcodes": ",".join(chunk), "deviceid": "Wap", "plat": "Wap",
                "product": "EFund", "version": "2.0.0", "_": int(time.time() * 1000),
            })
            try:
                text = self.client.get("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo?" + params, referer="https://fund.eastmoney.com/")
                batch = parse_funds(text, chunk)
            except FetchError as error:
                batch = Batch(errors={("fund", code): str(error) for code in chunk})
            merged.quotes.update(batch.quotes)
            merged.errors.update(batch.errors)
        return merged

    def coin(self, code: str) -> Batch:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(code, safe="") + "?interval=1d&range=5d"
        text = self.client.get(url, referer="https://finance.yahoo.com/")
        value = parse_yahoo(text, code)
        return Batch(quotes={value.key: value})
