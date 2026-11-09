import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .indicators import calculate_daily, supports_daily
from .models import DailyIndicators, Key, MarketTurnover, Quote, Row
from .providers import Providers


class Monitor:
    def __init__(self, config: Config, providers: Providers, clock=time.monotonic):
        self.config = config
        self.providers = providers
        self.clock = clock
        self.quotes: dict[Key, Quote] = {}
        self.errors: dict[Key, str] = {}
        self.last_fund_attempt: float | None = None
        self.indicators: dict[Key, DailyIndicators] = {}
        self.indicator_errors: dict[Key, str] = {}
        self.last_indicator_attempt: dict[Key, float] = {}
        self.market_turnover: MarketTurnover | None = None
        self.market_error = ""

    def refresh(self) -> list[Row]:
        groups = {kind: [a.code for a in self.config.assets if a.kind == kind] for kind in ("stock", "fund", "coin")}
        # A shared market snapshot is fetched once per cycle, not once per asset.
        jobs = [(self.providers.market_turnover, (), [], "market")]
        if groups["stock"]:
            jobs.append((self.providers.stocks, (groups["stock"],), [("stock", code) for code in groups["stock"]], "quote"))
        now = self.clock()
        if groups["fund"] and (self.last_fund_attempt is None or now - self.last_fund_attempt >= self.config.fund_interval_seconds):
            self.last_fund_attempt = now
            jobs.append((self.providers.funds, (groups["fund"],), [("fund", code) for code in groups["fund"]], "quote"))
        for code in groups["coin"]:
            jobs.append((self.providers.coin, (code,), [("coin", code)], "quote"))
        for asset in self.config.assets:
            attempted = self.last_indicator_attempt.get(asset.key)
            if supports_daily(asset) and (attempted is None or now - attempted >= 60):
                self.last_indicator_attempt[asset.key] = now
                jobs.append((self.providers.daily, (asset.code,), [asset.key], "daily"))
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(function, *arguments): (keys, kind) for function, arguments, keys, kind in jobs}
            for future in as_completed(futures):
                keys, kind = futures[future]
                try:
                    batch = future.result()
                    if kind == "market":
                        self.market_turnover = batch
                        self.market_error = ""
                        continue
                    if kind == "daily":
                        self.indicators[keys[0]] = calculate_daily(batch)
                        self.indicator_errors.pop(keys[0], None)
                        continue
                    for key in keys:
                        if key in batch.quotes:
                            self.quotes[key] = batch.quotes[key]
                            self.errors.pop(key, None)
                        else:
                            self.errors[key] = batch.errors.get(key, "行情源未返回数据")
                except Exception as error:
                    # One bad response must not stop the other markets or future cycles.
                    if kind == "market":
                        self.market_error = str(error) or type(error).__name__
                        continue
                    for key in keys:
                        errors = self.indicator_errors if kind == "daily" else self.errors
                        errors[key] = str(error) or type(error).__name__
        return [
            Row(
                asset, self.quotes.get(asset.key), self.errors.get(asset.key, ""),
                bool(self.quotes.get(asset.key) and self.errors.get(asset.key)),
                self.indicators.get(asset.key), self.indicator_errors.get(asset.key, ""),
                bool(self.indicators.get(asset.key) and self.indicator_errors.get(asset.key)),
            )
            for asset in self.config.assets
        ]
