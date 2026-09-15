from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from telemetry.collector import Collector, TelemetryError
from telemetry.config import Config, PollingConfig, RepeaterConfig


class ReauthenticationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repeater = RepeaterConfig(
            name="Hilltop",
            password="secret",
            public_key="0123456789abcdef",
        )
        config = Config(
            polling=PollingConfig(
                always_login=False,
                reauthenticate_interval_seconds=43200,
            ),
            repeaters=(self.repeater,),
        )
        self.collector = Collector(config, AsyncMock())
        self.collector._find_contact = lambda repeater: {"name": repeater.name}
        self.collector._apply_path = AsyncMock()
        self.collector._login = AsyncMock(
            side_effect=lambda _contact, repeater: self.collector._last_authenticated.__setitem__(
                repeater.key, 1000
            )
        )
        self.collector._request_telemetry = AsyncMock(return_value=[])
        self.collector._request_status = AsyncMock(return_value=None)

    @patch("telemetry.collector.time.monotonic", return_value=1000)
    async def test_logs_in_on_first_poll(self, _monotonic) -> None:
        await self.collector._poll_once(self.repeater)

        self.collector._login.assert_awaited_once()

    @patch("telemetry.collector.time.monotonic", return_value=2000)
    async def test_reuses_recent_authentication(self, _monotonic) -> None:
        self.collector._last_authenticated[self.repeater.key] = 1000

        await self.collector._poll_once(self.repeater)

        self.collector._login.assert_not_awaited()

    @patch("telemetry.collector.time.monotonic", return_value=44200)
    async def test_reauthenticates_after_twelve_hours(self, _monotonic) -> None:
        self.collector._last_authenticated[self.repeater.key] = 1000

        await self.collector._poll_once(self.repeater)

        self.collector._login.assert_awaited_once()

    @patch("telemetry.collector.time.monotonic", return_value=2000)
    async def test_failed_cached_session_forces_next_retry_login(self, _monotonic) -> None:
        self.collector._last_authenticated[self.repeater.key] = 1000
        self.collector._request_telemetry.side_effect = TelemetryError("no response")

        with self.assertRaises(TelemetryError):
            await self.collector._poll_once(self.repeater)

        self.assertNotIn(self.repeater.key, self.collector._last_authenticated)

    @patch("telemetry.collector.time.monotonic", return_value=2000)
    async def test_always_login_preserves_existing_behavior(self, _monotonic) -> None:
        self.collector.config = Config(
            polling=PollingConfig(always_login=True),
            repeaters=(self.repeater,),
        )
        self.collector._last_authenticated[self.repeater.key] = 1999

        await self.collector._poll_once(self.repeater)

        self.collector._login.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
