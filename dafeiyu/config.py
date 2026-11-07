import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .models import Asset, number


_PROJECT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"
DEFAULT_CONFIG = _PROJECT_CONFIG if _PROJECT_CONFIG.is_file() else Path(__file__).with_name("config.default.json")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    assets: tuple[Asset, ...]
    interval_seconds: float = 10
    fund_interval_seconds: float = 60
    timeout_seconds: float = 6
    stock_source: str = "tencent"
    proxy: str | None = None


def positive_seconds(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{label}必须是大于 0 的秒数")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{label}必须是大于 0 的秒数") from None
    if not math.isfinite(result) or result <= 0:
        raise ConfigError(f"{label}必须是大于 0 的秒数")
    return result


def normalize_code(kind: str, code: object) -> str:
    if not isinstance(code, str):
        raise ConfigError(f"{kind} 代码必须是字符串")
    code = code.strip()
    if kind == "stock":
        code = code[:2].lower() + code[2:]
        if code.startswith("us"):
            code = "us" + code[2:].upper()
        valid = re.fullmatch(r"(?:sh|sz|bj)\d{6}|hk\d{5}|us[A-Z0-9][A-Z0-9._-]*", code)
    elif kind == "fund":
        valid = re.fullmatch(r"\d{6}", code)
    else:
        code = code.upper()
        valid = re.fullmatch(r"[A-Z0-9][A-Z0-9._-]*-[A-Z]{3,5}", code)
    if not valid:
        raise ConfigError(f"无效的 {kind} 代码：{code}")
    return code


def parse_asset(kind: str, entry: object) -> Asset:
    if isinstance(entry, str):
        fields = [part.strip() for part in entry.split(",")]
        if len(fields) == 1:
            entry = {"code": fields[0]}
        elif len(fields) == 3:
            entry = dict(zip(("code", "cost_price", "quantity"), fields))
        else:
            raise ConfigError("持仓格式应为：代码,成本价,数量")
    if not isinstance(entry, dict):
        raise ConfigError("每个自选项必须是代码字符串或包含 code 的对象")
    unknown = set(entry) - {"code", "cost_price", "quantity"}
    if unknown:
        raise ConfigError(f"自选项包含未知字段：{', '.join(sorted(unknown))}")
    code = normalize_code(kind, entry.get("code"))
    values = {}
    for field in ("cost_price", "quantity"):
        raw = entry.get(field)
        if raw is None:
            values[field] = None
        else:
            parsed = number(raw)
            if parsed is None:
                raise ConfigError(f"{code} 的 {field} 必须是有效数字")
            if field == "quantity" and parsed < 0:
                raise ConfigError(f"{code} 的持仓数量不能小于 0")
            values[field] = parsed
    return Asset(kind, code, **values)


def parse_config(data: object) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("配置文件的最外层必须是 JSON 对象")
    allowed = {
        "interval_seconds", "fund_interval_seconds", "timeout_seconds",
        "stock_source", "proxy", "stocks", "funds", "coins",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"配置中包含未知字段：{', '.join(sorted(unknown))}")
    assets = []
    for group, kind in (("stocks", "stock"), ("funds", "fund"), ("coins", "coin")):
        entries = data.get(group, [])
        if not isinstance(entries, list):
            raise ConfigError(f"{group} 必须是 JSON 数组")
        seen = set()
        for entry in entries:
            asset = parse_asset(kind, entry)
            if asset.code in seen:
                raise ConfigError(f"{group} 中的代码重复：{asset.code}")
            seen.add(asset.code)
            assets.append(asset)
    if not assets:
        raise ConfigError("请至少配置一只股票、基金或加密货币")
    source = data.get("stock_source", "tencent")
    if not isinstance(source, str) or source not in {"tencent", "sina"}:
        raise ConfigError("stock_source 只能为 tencent 或 sina")
    if source == "sina" and any(a.kind == "stock" and a.code[:2] not in {"sh", "sz", "bj"} for a in assets):
        raise ConfigError("新浪备用源仅支持 A 股；港股和美股请选择 tencent")
    proxy = data.get("proxy")
    if proxy is not None:
        if not isinstance(proxy, str) or not proxy.strip():
            raise ConfigError("proxy 应为代理地址字符串，或 null")
        proxy = proxy.strip()
        if "://" not in proxy:
            proxy = "http://" + proxy
        try:
            parts = urlsplit(proxy)
            port = parts.port
        except ValueError:
            raise ConfigError("proxy 的地址或端口无效") from None
        if parts.scheme not in {"http", "https"} or not parts.hostname or port is None or port < 1:
            raise ConfigError("proxy 应类似 http://127.0.0.1:7890")
    return Config(
        assets=tuple(assets),
        interval_seconds=positive_seconds(data.get("interval_seconds", 10), "刷新间隔"),
        fund_interval_seconds=positive_seconds(data.get("fund_interval_seconds", 60), "基金请求间隔"),
        timeout_seconds=positive_seconds(data.get("timeout_seconds", 6), "请求超时"),
        stock_source=source,
        proxy=proxy,
    )


def read_config(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text)
    except FileNotFoundError:
        raise ConfigError(f"找不到配置文件：{path}") from None
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"无法读取配置文件：{error}") from None
    except json.JSONDecodeError as error:
        raise ConfigError(f"JSON 格式错误：第 {error.lineno} 行，第 {error.colno} 列") from None
    if not isinstance(data, dict):
        raise ConfigError("配置文件的最外层必须是 JSON 对象")
    return data
