import time
from collections.abc import Callable


def next_tick(previous_tick: float, now: float, interval: float) -> float:
    candidate = previous_tick + interval
    if candidate < now:
        missed = int((now - candidate) // interval) + 1
        candidate += missed * interval
    return candidate


def run_periodically(
    action: Callable[[], None], interval: float, count: int | None = None,
    *, clock=time.monotonic, sleep=time.sleep,
) -> None:
    tick = clock()
    completed = 0
    while count is None or completed < count:
        # Single-threaded scheduling prevents overlapping refresh cycles.
        action()
        completed += 1
        if count is not None and completed >= count:
            return
        tick = next_tick(tick, clock(), interval)
        delay = tick - clock()
        if delay > 0:
            sleep(delay)
