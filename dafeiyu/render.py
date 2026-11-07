import re
import shutil
from datetime import datetime
from decimal import Decimal, localcontext
from io import StringIO

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from .models import Row, position


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
    "kind": "类别", "code": "代码", "name": "名称", "change": "涨跌",
    "percent": "涨跌幅", "high": "最高", "low": "最低", "price": "现价/净值",
    "cost": "成本价", "quantity": "持仓", "return": "收益率", "income": "收益",
    "currency": "币种", "time": "行情时间", "status": "状态",
}
NUMERIC = {"change", "percent", "high", "low", "price", "cost", "quantity", "return", "income"}
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}Z?)$")


def column_widths(records: list[dict[str, Text]], keys: list[str], columns: int) -> dict[str, int]:
    sizes = {key: max(width(LABELS[key]), *(record[key].cell_len for record in records)) for key in keys}
    sizes["name"] = min(26, sizes["name"])
    # Only names may be shortened. Codes, timestamps and financial values stay intact.
    sizes["name"] = max(4, sizes["name"] - max(0, sum(sizes.values()) + len(keys) - 1 - columns))
    return sizes


def render_view(
    rows: list[Row], interval: float, *, now: datetime | None = None,
    columns: int | None = None,
) -> Group:
    """A compact quote/position table, rendered directly by Rich.Live."""
    now = now or datetime.now().astimezone()
    columns = max(24, columns if columns is not None else shutil.get_terminal_size((120, 24)).columns)
    parts = [Text(f"大飞鱼行情 | {now.strftime('%Y-%m-%d %H:%M:%S %z')}", style="bold cyan"), Text("")]
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
        records.append({
            "kind": Text(category), "code": Text(clean(asset.code)),
            "name": Text(clean(quote.name) if quote else "--"),
            "change": signed_cell(quote.change if quote else None, places=places),
            "percent": signed_cell(quote.change_percent if quote else None, percent=True),
            "high": Text(fmt(quote.high if quote else None, places=places)),
            "low": Text(fmt(quote.low if quote else None, places=places)),
            "price": Text(fmt(quote.price if quote else None, places=places), style="bold"),
            "cost": Text(quantity_text(asset.cost_price)), "quantity": Text(quantity_text(asset.quantity)),
            "return": signed_cell(result.income_percent, percent=True), "income": signed_cell(result.income),
            "currency": Text(clean(quote.currency) if quote else "--"), "time": Text(clean(stamp)),
            "status": Text(status, style="yellow" if row.stale or row.error else ""),
        })

    keys = ["code", "name", "change", "percent", "high", "low", "price", "cost", "quantity", "return", "income", "time"]
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
        hidden = ["change", "high", "low"]
        keys = [key for key in keys if key not in hidden]
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
        # Keep complete names available even when the one-line table abbreviates them.
        names = [f"{record['code'].plain} {record['name'].plain}" for record in records if record["name"].cell_len > sizes["name"]]
        if names:
            parts.append(Text("名称：" + "；".join(names), style="dim"))
        if hidden:
            parts.append(Text("窗口较窄，以下补充涨跌及高低价；拉宽窗口可合并到表格。", style="dim"))
            for record in records:
                parts.append(Text(" | ".join([record["code"].plain, *(f"{LABELS[key]} {record[key].plain}" for key in hidden)]), style="dim"))
    else:
        parts.append(Text("窗口较窄，已逐项展示；拉宽窗口可查看表格。", style="yellow"))
        for record in records:
            parts.append(Text("─" * columns, style="dim"))
            for key in [*keys, *hidden]:
                parts.append(Text(f"{LABELS[key]}：{record[key].plain}"))

    for row in rows:
        if row.error:
            parts.append(Text(f"! {row.asset.kind}/{row.asset.code}: {clip(row.error, 180)}", style="yellow"))
    if any(row.asset.kind == "coin" for row in rows):
        parts.append(Text("加密货币时间以 Z 标示 UTC。", style="dim"))
    if any(row.asset.kind == "fund" for row in rows):
        parts.append(Text("基金按配置的独立间隔请求；净值/估值类型见类别列。", style="dim"))
    return Group(*parts)


def render(
    rows: list[Row], interval: float, *, now: datetime | None = None,
    columns: int | None = None,
) -> str:
    """Plain snapshot for scripts and tests, using the same layout as the live view."""
    columns = max(24, columns if columns is not None else shutil.get_terminal_size((120, 24)).columns)
    stream = StringIO()
    console = Console(file=stream, width=columns, color_system=None, force_terminal=False)
    console.print(render_view(rows, interval, now=now, columns=columns))
    return stream.getvalue().rstrip("\n")
