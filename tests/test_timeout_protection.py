from __future__ import annotations

import unittest

import httpx

from timeout_protection import TimeoutProtection


class TimeoutProtectionTests(unittest.TestCase):
    def test_retries_transient_request_errors(self) -> None:
        request = httpx.Request("GET", "https://karakeep.example.test")
        calls = 0
        logs: list[str] = []

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ReadTimeout("timed out", request=request)
            return "ok"

        protection = TimeoutProtection(
            log=lambda message, **_kwargs: logs.append(message),
            checkpoint=lambda: None,
            auto_pause=lambda _message: self.fail("should not auto-pause"),
        )

        self.assertEqual(protection.run("test request", operation), "ok")
        self.assertEqual(calls, 3)
        self.assertEqual(len(logs), 2)

    def test_auto_pauses_after_three_failures_then_resumes(self) -> None:
        request = httpx.Request("GET", "https://karakeep.example.test")
        calls = 0
        pauses: list[str] = []

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise httpx.ConnectError("connection dropped", request=request)
            return "resumed"

        protection = TimeoutProtection(
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
            auto_pause=lambda message: pauses.append(message),
        )

        self.assertEqual(protection.run("test request", operation), "resumed")
        self.assertEqual(calls, 4)
        self.assertEqual(len(pauses), 1)
        self.assertIn("paused after 3 failed attempts", pauses[0])
