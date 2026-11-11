"""Thread-safe, content-free usage counters; SDK retries remain one logical client call."""

from dataclasses import asdict, dataclass
from threading import Lock


@dataclass(frozen=True)
class APIUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_missing: int = 0

    def since(self, before: "APIUsage") -> "APIUsage":
        return APIUsage(*(getattr(self, key) - getattr(before, key) for key in asdict(self)))


class UsageCounter:
    def __init__(self):
        self._lock = Lock()
        self._values = [0, 0, 0, 0]

    def started(self) -> None:
        with self._lock:
            self._values[0] += 1
            self._values[3] += 1

    def received(self, input_tokens, output_tokens=0) -> None:
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
            return
        with self._lock:
            self._values[1] += input_tokens
            self._values[2] += output_tokens
            self._values[3] -= 1

    def snapshot(self) -> APIUsage:
        with self._lock:
            return APIUsage(*self._values)
