import argparse
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG, ConfigError, parse_config, read_config
from .engine import Monitor
from .providers import HttpClient, Providers
from .scheduler import run_periodically


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="大飞鱼：股票、基金、加密货币控制台行情与持仓收益。")
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="JSON 配置文件路径")
    result.add_argument("--interval", type=float, help="刷新间隔，单位秒，默认使用配置中的 10 秒")
    result.add_argument("--timeout", type=float, help="单次网络请求超时秒数")
    result.add_argument("--stock-source", choices=("tencent", "sina"), help="股票源；新浪仅支持 A 股")
    result.add_argument("--stocks", nargs="*", help="覆盖股票自选列表，例如 --stocks sh000001 hk00700 usAAPL")
    result.add_argument("--funds", nargs="*", help="覆盖基金列表，例如 --funds 110013")
    result.add_argument("--coins", nargs="*", help="覆盖币种列表，例如 --coins BTC-USD DOGE-USD")
    result.add_argument("--display", choices=("auto", "live", "log"), default="auto", help="显示方式：auto 自动选择；live 强制原地刷新；log 逐轮输出")
    result.add_argument("--score-details", action="store_true", help="展开技术分明细；Windows 终端原地刷新时也可按 D 开关")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="获取并输出一轮后退出")
    mode.add_argument("--count", type=int, help="输出指定轮数后退出")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        data = read_config(args.config)
        for name in ("stocks", "funds", "coins", "stock_source"):
            value = getattr(args, name)
            if value is not None:
                data[name] = value
        if args.interval is not None:
            data["interval_seconds"] = args.interval
        if args.timeout is not None:
            data["timeout_seconds"] = args.timeout
        if args.count is not None and args.count < 1:
            raise ConfigError("--count 必须大于等于 1")
        config = parse_config(data)
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    try:
        from .display import Dashboard
        from .render import render_view
        from .presentation import SignalChanges
    except ModuleNotFoundError as error:
        if error.name and error.name.split(".")[0] in {"rich", "markdown_it", "mdurl", "pygments"}:
            print("缺少显示依赖。请使用当前 Python 执行：python -m pip install -r requirements.txt", file=sys.stderr)
            return 2
        raise
    monitor = Monitor(config, Providers(HttpClient(config.timeout_seconds, config.proxy), config.stock_source))
    last_rows = []
    changes = SignalChanges()
    try:
        with Dashboard(args.display, single=args.once or args.count == 1, score_details=args.score_details) as display:
            def repaint() -> None:
                display.publish(render_view(
                    last_rows, config.interval_seconds, columns=display.columns,
                    market=monitor.market_turnover, market_error=monitor.market_error,
                    score_details=display.score_details, interactive=display.key_enabled,
                    highlighted=changes.active() if display.enabled else None,
                ))

            def refresh() -> None:
                nonlocal last_rows
                display.begin_refresh()
                last_rows = monitor.refresh()
                changes.observe(last_rows)
                repaint()

            def poll_ui(force: bool) -> None:
                expired = changes.expire()
                if force or expired:
                    repaint()

            run_periodically(
                refresh, config.interval_seconds, 1 if args.once else args.count,
                sleep=lambda seconds: display.wait(seconds, on_poll=poll_ui),
            )
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    except BrokenPipeError:
        return 0
    # One-shot and bounded runs are useful for terminal scripts and health checks.
    return 1 if last_rows and all(row.error for row in last_rows) else 0
