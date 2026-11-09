import re
import shutil
from datetime import datetime
from decimal import Decimal, localcontext
from io import StringIO

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from .assessment import assess_row
from .models import Key, MarketTurnover, Row, position
from .presentation import kdj_label, macd_label


def width(value: str) -> int:
    return Text(value).cell_len


def clean(value: object) -> str:
    return "".join(character if character.isprintable() else " " for character in str(value))


def clip(value: object, limit: int) -> str:
    text = clean(value)
    if width(text) <= limit:
        return text
    marker = "." * min(3, max(0, limit))
    result = ""
    for character in text:
        if width(result + character) > limit - len(marker):
            break
        result += character
    return result + marker


def fmt(value: Decimal | None, *, places: int = 2, signed: bool = False) -> str:
    if value is None:
        return "--"
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + abs(value.adjusted()) + places + 5)
        return format(value, f"{'+' if signed else ''},.{places}f")


def quantity_text(value: Decimal | None) -> str:
    if value is None:
        return "--"
    text = format(value, ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def price_places(kind: str, value: Decimal | None) -> int:
    if kind == "fund":
        return 4
    if value is None:
        return 2
    return max(2, min(8, -value.as_tuple().exponent))


def signed_cell(value: Decimal | None, *, places: int = 2, percent: bool = False) -> Text:
    text = fmt(value, places=places, signed=True)
    if value is not None and percent:
        text += "%"
    return Text(text, style="red" if value is not None and value > 0 else "green" if value is not None and value < 0 else "")


LABELS = {
    "kind": "类别", "code": "代码", "name": "名称",
    "percent": "涨跌幅", "high": "最高", "low": "最低", "price": "现价/净值",
    "cost": "成本价", "quantity": "持仓", "return": "收益率", "income": "收益",
    "kdj": "KDJ(日)", "macd": "MACD(日)", "indicator_date": "指标日期",
    "rating": "技术分",
    "currency": "币种", "time": "行情时间", "status": "状态",
}
NUMERIC = {"percent", "high", "low", "price", "cost", "quantity", "return", "income", "rating"}
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}Z?)$")


def indicator_cells(row: Row, *, show_provisional: bool = True) -> dict[str, Text]:
    indicator = row.indicators
    if indicator is None:
        return {key: Text("--") for key in ("kdj", "macd", "rating", "indicator_date")}
    flags = (["预"] if indicator.provisional and show_provisional else []) + (["旧"] if row.indicators_stale else [])
    suffix = "(" + "/".join(flags) + ")" if flags else ""
    macd_signal = indicator.macd_signal or indicator.macd_color
    kdj_style = "yellow" if indicator.kdj_signal.startswith("即将") else "red" if indicator.kdj_signal == "金叉" else "green" if indicator.kdj_signal == "死叉" else "dim"
    macd_style = "yellow" if macd_signal.startswith("即将") else "red" if indicator.macd > 0 else "green" if indicator.macd < 0 else "dim"
    assessment = assess_row(row)
    if assessment.score is None:
        rating_style = "yellow" if assessment.label == "待更新" else "dim"
    else:
        rating_style = "red" if assessment.score > 0 else "green" if assessment.score < 0 else "dim"
        if abs(assessment.score) >= 7:
            rating_style = "bold " + rating_style
    rating_suffix = "(预)" if indicator.provisional and show_provisional and assessment.score is not None else ""
    return {
        "kdj": Text(kdj_label(indicator) + suffix, style=kdj_style),
        "macd": Text(macd_label(indicator) + suffix, style=macd_style),
        "rating": Text(assessment.label + rating_suffix, style=rating_style),
        "indicator_date": Text(indicator.as_of),
    }


def market_cell(market: MarketTurnover | None, error: str, now: datetime) -> Text:
    if market is None:
        return Text("--", style="dim")
    change = market.change_amount
    flags = []
    change_style = "dim"
    if change is None:
        flags.append("对比--")
    else:
        amount = fmt(abs(change) / Decimal(100_000_000))
        kind = market.comparison_kind
        if Decimal(amount.replace(",", "")) == 0:
            flags.append(kind + "持平")
        else:
            flags.append(kind + ("增量" if change > 0 else "缩量") + amount + "亿")
            change_style = "red" if change > 0 else "green"
    if market.market_time.date() != now.astimezone(market.market_time.tzinfo).date():
        flags.append(market.market_time.strftime("%m-%d"))
    if error:
        flags.append("旧")
    result = Text(fmt(market.total_amount / Decimal(100_000_000)), style="yellow" if error else "cyan")
    result.append("(" + "/".join(flags) + ")", style="yellow" if error else change_style)
    return result


def column_widths(records: list[dict[str, Text]], keys: list[str], columns: int) -> dict[str, int]:
    sizes = {key: max(width(LABELS[key]), *(record[key].cell_len for record in records)) for key in keys}
    sizes["name"] = min(26, sizes["name"])
    # Only names may be shortened. Codes, timestamps and financial values stay intact.
    sizes["name"] = max(4, sizes["name"] - max(0, sum(sizes.values()) + len(keys) - 1 - columns))
    return sizes


def market_summary(market: MarketTurnover | None, error: str = "") -> Text:
    result = Text("沪深成交额 ", style="dim")
    if market is None:
        result.append("-- | 前日同期对比 --")
        return result
    result.append(fmt(market.total_amount / Decimal(100_000_000)) + "亿元", style="bold cyan")
    change = market.change_amount
    if change is None:
        result.append(" | 前日同期对比 --")
    else:
        style = "red" if change > 0 else "green" if change < 0 else "dim"
        kind = market.comparison_kind or "同期"
        direction = "增量" if change > 0 else "缩量" if change < 0 else "持平"
        result.append(" | 较前交易日" + kind + " ")
        result.append(direction + " " + fmt(abs(change) / Decimal(100_000_000)) + "亿元", style=style)
        if market.previous_amount is not None and market.previous_amount > 0:
            result.append(" (" + fmt(change / market.previous_amount * 100, signed=True) + "%)", style=style)
        else:
            result.append(" (幅度--)")
    result.append(" | 截至 " + market.market_time.strftime("%m-%d %H:%M"))
    if error:
        result.append(" (旧)", style="yellow")
    return result


def render_view(
    rows: list[Row], interval: float, *, now: datetime | None = None,
    columns: int | None = None, market: MarketTurnover | None = None, market_error: str = "",
    score_details: bool = False, highlighted: set[tuple[Key, str]] | None = None,
    interactive: bool = False,
) -> Group:
    """A compact quote/position table, rendered directly by Rich.Live."""
    now = now or datetime.now().astimezone()
    columns = max(24, columns if columns is not None else shutil.get_terminal_size((120, 24)).columns)
    title = Text(f"大飞鱼行情 | {now.strftime('%Y-%m-%d %H:%M:%S %z')}", style="bold cyan")
    known = [row.indicators for row in rows if row.indicators is not None]
    pre_count = sum(indicator.provisional for indicator in known)
    uniform_pre = bool(known) and pre_count == len(known)
    if uniform_pre:
        title.append(" | 盘中预估", style="yellow")
    elif pre_count:
        title.append(" | 部分盘中预估", style="yellow")
    elif known:
        title.append(" | 收盘日线", style="dim")
    if interactive:
        title.append(" | D " + ("收起明细" if score_details else "评分明细"), style="dim")
    parts = [title, market_summary(market, market_error), Text("")]
    if not rows:
        return Group(*parts, Text("暂无自选标的。"))

    dates = {row.quote.market_time[:10] for row in rows if row.quote and re.match(r"^\d{4}-\d{2}-\d{2}", row.quote.market_time)}
    common_date = next(iter(dates)) if len(dates) == 1 else None
    records = []
    for row in rows:
        asset, quote = row.asset, row.quote
        result = position(asset, quote)
        places = price_places(asset.kind, quote.price if quote else None)
        category = {"stock": "股票", "fund": "基金", "coin": "加密币"}[asset.kind]
        if quote and asset.kind == "fund":
            category += "/" + quote.price_kind
        stamp = quote.market_time if quote else "--"
        match = STAMP.fullmatch(stamp)
        if match and match[1] == common_date:
            stamp = match[2]
        status = "更新失败/旧数据" if row.stale else "获取失败" if row.error else "正常"
        name = clean(quote.name).strip() if quote else "--"
        name = (name[2:4] if name.startswith("科创") else name[:2]) or "--"
        record = {
            "kind": Text(category), "code": Text(clean(asset.code)),
            "name": Text(name),
            "percent": signed_cell(quote.change_percent if quote else None, percent=True),
            "high": Text(fmt(quote.high if quote else None, places=places)),
            "low": Text(fmt(quote.low if quote else None, places=places)),
            "price": Text(fmt(quote.price if quote else None, places=places), style="bold"),
            "cost": Text(quantity_text(asset.cost_price)), "quantity": Text(quantity_text(asset.quantity)),
            "return": signed_cell(result.income_percent, percent=True), "income": signed_cell(result.income),
            "currency": Text(clean(quote.currency) if quote else "--"), "time": Text(clean(stamp)),
            "status": Text(status, style="yellow" if row.stale or row.error else ""),
            **indicator_cells(row, show_provisional=not uniform_pre),
        }
        changed = {column for key, column in (highlighted or set()) if key == asset.key}
        for column in changed:
            if column in record:
                record[column].stylize("bold reverse")
        if changed:
            record["code"].stylize("bold yellow")
        records.append(record)

    keys = ["code", "name", "percent", "high", "low", "price", "cost", "quantity", "return", "income", "kdj", "macd", "rating", "indicator_date", "time"]
    if any(row.asset.kind != "stock" for row in rows):
        keys.insert(0, "kind")
    currencies = {row.quote.currency for row in rows if row.quote}
    if currencies != {"CNY"}:
        keys.insert(-1, "currency")
    if any(row.stale or row.error for row in rows):
        keys.append("status")
    sizes = column_widths(records, keys, columns)
    hidden = []
    if sum(sizes.values()) + len(keys) - 1 > columns:
        hidden = ["high", "low"]
        keys = [key for key in keys if key not in hidden]
        sizes = column_widths(records, keys, columns)
    for key in ("time", "indicator_date", "cost", "quantity", "return"):
        if sum(sizes.values()) + len(keys) - 1 <= columns:
            break
        hidden.append(key)
        keys.remove(key)
        sizes = column_widths(records, keys, columns)

    if sum(sizes.values()) + len(keys) - 1 <= columns:
        grid = Table(
            box=box.MINIMAL, show_edge=False, padding=0, collapse_padding=True,
            header_style="bold white on grey19", border_style="grey35",
            row_styles=["", "on grey11"], highlight=False,
        )
        for key in keys:
            grid.add_column(Text(LABELS[key]), width=sizes[key], no_wrap=True, overflow="ellipsis",
                            justify="right" if key in NUMERIC else "left")
        for record in records:
            grid.add_row(*(record[key] for key in keys))
        parts.append(grid)
        if hidden:
            parts.append(Text("窗口较窄，其余字段在下方补充；拉宽窗口可合并到表格。", style="dim"))
            for record in records:
                parts.append(Text(" | ".join([record["code"].plain, *(f"{LABELS[key]} {record[key].plain}" for key in hidden)]), style="dim"))
    else:
        parts.append(Text("窗口较窄，已逐项展示；拉宽窗口可查看表格。", style="yellow"))
        for record in records:
            parts.append(Text("─" * columns, style="dim"))
            for key in [*keys, *hidden]:
                parts.append(Text(f"{LABELS[key]}：{record[key].plain}"))

    if score_details:
        parts.extend([Text(""), Text("技术分明细", style="bold")])
        for row, record in zip(rows, records):
            assessment = assess_row(row)
            indicator = row.indicators
            numbers = ""
            if indicator is not None:
                numbers = " | K/D/J " + "/".join(fmt(value) for value in (indicator.k, indicator.d, indicator.j))
                if indicator.kdj_cross_day:
                    numbers += " | 最近交叉 " + indicator.kdj_cross_day
            parts.append(Text(f"{record['name'].plain} {row.asset.code} {record['rating'].plain}{numbers}", style="bold"))
            parts.append(Text("  " + clean(assessment.reason), style="dim"))

    if market_error:
        parts.append(Text(f"! 大A量能：{clip(market_error, 180)}", style="yellow"))
    for row in rows:
        if row.error:
            parts.append(Text(f"! {row.asset.kind}/{row.asset.code}: {clip(row.error, 180)}", style="yellow"))
        if row.indicator_error:
            parts.append(Text(f"! {row.asset.code} 指标：{clip(row.indicator_error, 180)}", style="yellow"))
    if any(row.asset.kind == "coin" for row in rows):
        parts.append(Text("加密货币时间以 Z 标示 UTC。", style="dim"))
    if any(row.asset.kind == "fund" for row in rows):
        parts.append(Text("基金按配置的独立间隔请求；净值/估值类型见类别列。", style="dim"))
    return Group(*parts)


def render(
    rows: list[Row], interval: float, *, now: datetime | None = None,
    columns: int | None = None, market: MarketTurnover | None = None, market_error: str = "",
    score_details: bool = False, highlighted: set[tuple[Key, str]] | None = None,
    interactive: bool = False,
) -> str:
    """Plain snapshot for scripts and tests, using the same layout as the live view."""
    columns = max(24, columns if columns is not None else shutil.get_terminal_size((120, 24)).columns)
    stream = StringIO()
    console = Console(file=stream, width=columns, color_system=None, force_terminal=False)
    console.print(render_view(
        rows, interval, now=now, columns=columns, market=market, market_error=market_error,
        score_details=score_details, highlighted=highlighted, interactive=interactive,
    ))
    return stream.getvalue().rstrip("\n")
