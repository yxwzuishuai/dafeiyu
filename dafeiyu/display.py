"""Terminal display lifecycle, independent from market data and scheduling."""

import os
import sys
import time

from rich.console import Console, RenderableType
from rich.live import Live
from rich.text import Text


def windows_key() -> str | None:
    if os.name != "nt" or sys.stdin is None or not sys.stdin.isatty():
        return None
    import msvcrt
    if not msvcrt.kbhit():
        return None
    key = msvcrt.getwch()
    if key in {"\x00", "\xe0"}:
        msvcrt.getwch()
        return None
    return key


class Dashboard:
    def __init__(self, mode: str = "auto", *, single: bool = False, console: Console | None = None,
                 score_details: bool = False, key_reader=None):
        self.console = console or Console(force_terminal=True if mode == "live" else None)
        terminal = self.console.is_terminal and not self.console.is_dumb_terminal
        self.enabled = not single and mode != "log" and terminal
        self.show_hint = mode != "log" and not single and not terminal
        self._live: Live | None = None
        self._body: RenderableType | None = None
        self.score_details = score_details
        self.key_enabled = self.enabled and (key_reader is not None or (os.name == "nt" and sys.stdin is not None and sys.stdin.isatty()))
        self._key_reader = key_reader or windows_key

    @property
    def columns(self) -> int:
        return self.console.size.width

    def __enter__(self):
        if self.enabled:
            self._live = Live(
                console=self.console, screen=True, auto_refresh=False,
                redirect_stdout=False, redirect_stderr=False,
                vertical_overflow="ellipsis",
            )
            self._live.__enter__()
        elif self.show_hint:
            self.console.print(Text(
                "提示：当前是普通输出窗口，使用逐轮输出。"
                "原地刷新请在 IDE 的 Terminal 或 Windows Terminal 中运行 .\\run.bat。"
            ), style="yellow")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._live is not None:
            # Live restores the terminal and cursor even when Ctrl+C interrupts a request.
            self._live.__exit__(exc_type, exc_value, traceback)
            self._live = None
        return False

    def begin_refresh(self) -> None:
        if self._live is not None and self._body is not None:
            self._live.update(self._body, refresh=True)

    def publish(self, body: RenderableType) -> None:
        self._body = Text(body) if isinstance(body, str) else body
        if self._live is not None:
            self._live.update(self._body, refresh=True)
        else:
            self.console.print(self._body)

    def wait(self, seconds: float, *, on_poll, clock=time.monotonic, sleep=time.sleep) -> None:
        if not self.enabled:
            sleep(seconds)
            return
        deadline = clock() + seconds
        while clock() < deadline:
            changed = False
            key = self._key_reader() if self.key_enabled else None
            if key is not None and key.lower() == "d":
                self.score_details = not self.score_details
                changed = True
            on_poll(changed)
            remaining = deadline - clock()
            if remaining > 0:
                sleep(min(0.2, remaining))
