from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import httpx
from PySide6.QtCore import QThread


T = TypeVar("T")


class TimeoutProtection:
    """Retries transient network failures and auto-pauses after repeated failures."""

    def __init__(
        self,
        *,
        log: Callable[..., None],
        checkpoint: Callable[[], None],
        auto_pause: Callable[[str], None],
        attempts_before_pause: int = 3,
    ) -> None:
        self.log = log
        self.checkpoint = checkpoint
        self.auto_pause = auto_pause
        self.attempts_before_pause = max(1, attempts_before_pause)

    def run(self, description: str, operation: Callable[[], T]) -> T:
        attempt = 0

        while True:
            self.checkpoint()
            try:
                return operation()
            except httpx.RequestError as exc:
                attempt += 1
                if attempt < self.attempts_before_pause:
                    self.log(
                        "Timeout protection retry "
                        f"{attempt}/{self.attempts_before_pause} while "
                        f"{description}: {exc}",
                        level="WARNING",
                        message_color="WARNING",
                    )
                    QThread.msleep(500 * attempt)
                    continue

                message = (
                    "Timeout protection paused after "
                    f"{attempt} failed attempts while {description}: {exc}. "
                    "Check the server or network, then press Unpause to resume."
                )
                self.log(message, level="ERROR", message_color="ERROR")
                self.auto_pause(message)
                self.checkpoint()
                attempt = 0
