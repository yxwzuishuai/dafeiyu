import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .models import Key, Quote, Row
from .providers import Providers


class Monitor:
    def __init__(self, config: Config, providers: Providers, clock=time.monotonic):
        self.config = config
        self.providers = providers
        self.clock = clock
        self.quotes: dict[Key, Quote] = {}
        self.errors: dict[Key, str] = {}
        self.last_fund_attempt: float | None = None

    def refresh(self) -> list[Row]:
        groups = {kind: [a.code for a in self.config.assets if a.kind == kind] for kind in ("stock", "fund", "coin")}
        jobs = []
        if groups["stock"]:
            jobs.append((self.providers.stocks, groups["stock"], [("stock", code) for code in groups["stock"]]))
        now = self.clock()
        if groups["fund"] and (self.last_fund_attempt is None or now - self.last_fund_attempt >= self.config.fund_interval_seconds):
            self.last_fund_attempt = now
            jobs.append((self.providers.funds, groups["fund"], [("fund", code) for code in groups["fund"]]))
        for code in groups["coin"]:
            jobs.append((self.providers.coin, code, [("coin", code)]))
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(function, argument): keys for function, argument, keys in jobs}
            for future in as_completed(futures):
                keys = futures[future]
                try:
                    batch = future.result()
                    for key in keys:
                        if key in batch.quotes:
                            self.quotes[key] = batch.quotes[key]
                            self.errors.pop(key, None)
                        else:
                            self.errors[key] = batch.errors.get(key, "行情源未返回数据")
                except Exception as error:
                    # One bad response must not stop the other markets or future cycles.
                    for key in keys:
                        self.errors[key] = str(error) or type(error).__name__
        return [
            Row(asset, self.quotes.get(asset.key), self.errors.get(asset.key, ""), bool(self.quotes.get(asset.key) and self.errors.get(asset.key)))
            for asset in self.config.assets
        ]
