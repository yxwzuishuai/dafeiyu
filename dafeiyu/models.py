from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


Key = tuple[str, str]


def number(value: object) -> Decimal | None:
    """Missing and non-finite market values must never become a price of zero."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").removesuffix("%")
    if text in {"", "-", "--", "null", "None", "N/A"}:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


@dataclass(frozen=True)
class Asset:
    kind: str
    code: str
    cost_price: Decimal | None = None
    quantity: Decimal | None = None

    @property
    def key(self) -> Key:
        return self.kind, self.code


@dataclass(frozen=True)
class Quote:
    kind: str
    code: str
    name: str
    price: Decimal
    change: Decimal | None
    change_percent: Decimal | None
    high: Decimal | None
    low: Decimal | None
    currency: str
    market_time: str
    source: str
    price_kind: str = "现价"

    @property
    def key(self) -> Key:
        return self.kind, self.code


@dataclass(frozen=True)
class Position:
    income: Decimal | None
    income_percent: Decimal | None


def position(asset: Asset, quote: Quote | None) -> Position:
    if quote is None or asset.cost_price is None:
        return Position(None, None)
    difference = quote.price - asset.cost_price
    income = difference * asset.quantity if asset.quantity is not None else None
    # A zero or negative remaining cost has no conventional percentage return.
    percentage = difference / asset.cost_price * 100 if asset.cost_price > 0 else None
    return Position(income, percentage)


@dataclass(frozen=True)
class DailyBar:
    day: str
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class DailyIndicators:
    as_of: str
    k: Decimal
    d: Decimal
    j: Decimal
    dif: Decimal
    dea: Decimal
    macd: Decimal
    kdj_signal: str
    provisional: bool = False

    @property
    def macd_color(self) -> str:
        return "红柱" if self.macd > 0 else "绿柱" if self.macd < 0 else "零轴"


@dataclass(frozen=True)
class Row:
    asset: Asset
    quote: Quote | None
    error: str = ""
    stale: bool = False
    indicators: DailyIndicators | None = None
    indicator_error: str = ""
    indicators_stale: bool = False
