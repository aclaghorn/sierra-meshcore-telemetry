from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from telemetry.config import ConfigError, HttpEndpointConfig, PublishingConfig, load_config
from telemetry.db import Database
from telemetry.publisher import HttpPublisher, PublicationError

ENDPOINT = HttpEndpointConfig(
    name="Test Grid",
    url="https://example.invalid/api/v1/ingest/mesh.repeater",
    bearer_token="secret-token",
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class FakeOpener:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.requests: list[Any] = []
        self.responses = responses or []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        response = (
            self.responses.pop(0)
            if self.responses
            else FakeResponse({"accepted": 1})
        )
        if isinstance(response, Exception):
            raise response
        return response


def http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        ENDPOINT.url, code, "error", headers or {}, None  # type: ignore[arg-type]
    )


class HttpPublisherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "telemetry.db")
        await self.db.connect()
        await self.db.register_repeater(
            "0123456789abcdef", "Hilltop", "0123456789abcdef", "deadbeef"
        )
        await self.db.record_reading(
            "0123456789abcdef",
            "Hilltop",
            [{"type": "voltage", "value": 3.91}, {"type": "temperature", "value": 18.4}],
            attempt=1,
            ts=1_789_416_000,
        )
        await self.db.record_stats(
            "0123456789abcdef",
            "Hilltop",
            {"uptime": 2000, "nb_sent": 17, "bat": 3910},
            ts=1_789_416_000,
        )
        await self.db.record_attempt(
            "0123456789abcdef", "Hilltop", True, 1, None, ts=1_789_416_000
        )
        # Never reached, and reported anyway.
        await self.db.register_repeater(
            "fedcba9876543210", "Lilac Park", "fedcba9876543210", None
        )
        await self.db.record_attempt(
            "fedcba9876543210", "Lilac Park", False, 3, "no response", ts=1_789_416_000
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    async def _publish(self, opener: FakeOpener, now: int = 1_789_416_100) -> None:
        await HttpPublisher((ENDPOINT,), self.db, opener).publish(now=now)

    async def test_posts_report_keyed_by_public_key(self) -> None:
        opener = FakeOpener()

        await self._publish(opener)

        request = opener.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, ENDPOINT.url)
        self.assertEqual(request.headers["Authorization"], "Bearer secret-token")
        self.assertEqual(request.headers["Content-type"], "application/json")

        report = json.loads(request.data)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["generated_at"], "2026-09-14T20:01:40+00:00")
        by_id = {entry["id"]: entry for entry in report["repeaters"]}
        self.assertEqual(set(by_id), {"0123456789abcdef", "fedcba9876543210"})

        hilltop = by_id["0123456789abcdef"]
        self.assertEqual(hilltop["name"], "Hilltop")
        self.assertEqual(hilltop["last_success"], 1_789_416_000)
        self.assertEqual(hilltop["battery_voltage"], 3.91)
        self.assertEqual(hilltop["battery_percent_source"], "estimated")
        self.assertEqual(hilltop["uptime_s"], 2000)
        self.assertEqual(hilltop["nb_sent"], 17)
        self.assertTrue(hilltop["online"])
        self.assertNotIn("public_key", hilltop)
        self.assertNotIn("path", hilltop)

    async def test_unreachable_repeater_reports_nulls_not_zeroes(self) -> None:
        opener = FakeOpener()

        await self._publish(opener)

        report = json.loads(opener.requests[0].data)
        lilac = next(
            entry for entry in report["repeaters"] if entry["id"] == "fedcba9876543210"
        )
        self.assertEqual(lilac["last_attempt"], 1_789_416_000)
        self.assertIsNone(lilac["last_success"])
        self.assertIsNone(lilac["battery_percent"])
        self.assertIsNone(lilac["nb_sent"])
        self.assertIsNone(lilac["uptime_s"])
        self.assertFalse(lilac["online"])

    async def test_measured_battery_percent_uses_api_vocabulary(self) -> None:
        await self.db.record_reading(
            "0123456789abcdef",
            "Hilltop",
            [{"type": "voltage", "value": 3.91}, {"type": "percentage", "value": 62.0}],
            attempt=1,
            ts=1_789_416_050,
        )
        opener = FakeOpener()

        await self._publish(opener)

        report = json.loads(opener.requests[0].data)
        hilltop = next(
            entry for entry in report["repeaters"] if entry["id"] == "0123456789abcdef"
        )
        self.assertEqual(hilltop["battery_percent"], 62.0)
        self.assertEqual(hilltop["battery_percent_source"], "measured")

    async def test_skips_repeaters_without_a_hex_public_key(self) -> None:
        await self.db.register_repeater("kitchen table", "Kitchen Table", None, None)
        opener = FakeOpener()

        with self.assertLogs("telemetry.publisher", level="WARNING"):
            await self._publish(opener)

        report = json.loads(opener.requests[0].data)
        self.assertEqual(len(report["repeaters"]), 2)

    async def test_retries_rate_limited_report_then_succeeds(self) -> None:
        opener = FakeOpener(
            [http_error(429, {"Retry-After": "0"}), FakeResponse({"accepted": 2})]
        )

        with self.assertLogs("telemetry.publisher", level="WARNING"):
            await self._publish(opener)

        self.assertEqual(len(opener.requests), 2)

    async def test_gives_up_after_repeated_server_errors(self) -> None:
        opener = FakeOpener([http_error(503) for _ in range(4)])

        with self.assertRaises(PublicationError):
            with patch("telemetry.publisher.asyncio.sleep", AsyncMock()):
                await self._publish(opener)

        self.assertEqual(len(opener.requests), 4)

    async def test_does_not_retry_rejected_report(self) -> None:
        opener = FakeOpener([http_error(401)])

        with self.assertRaises(PublicationError) as caught:
            await self._publish(opener)

        self.assertIn("401", str(caught.exception))
        self.assertEqual(len(opener.requests), 1)

    async def test_logs_warnings_returned_by_the_api(self) -> None:
        opener = FakeOpener(
            [FakeResponse({"accepted": 1, "warnings": ["repeaters[1]: skipped"]})]
        )

        with self.assertLogs("telemetry.publisher", level="WARNING") as logs:
            await self._publish(opener)

        self.assertIn("repeaters[1]: skipped", "\n".join(logs.output))


class HttpEndpointConfigTest(unittest.TestCase):
    def _write(self, publishing: str) -> Path:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "config.yaml"
        path.write_text(
            publishing
            + "\nrepeaters:\n  - name: Hilltop\n    public_key: abcd\n"
            "    password: secret\n",
            encoding="utf-8",
        )
        return path

    def test_parses_endpoints(self) -> None:
        path = self._write(
            "publishing:\n"
            "  enabled: true\n"
            "  http_endpoints:\n"
            "    - name: Grid\n"
            "      url: https://example.invalid/ingest\n"
            "      bearer_token: token\n"
        )

        config = load_config(path)

        self.assertEqual(
            config.publishing.http_endpoints,
            (
                HttpEndpointConfig(
                    name="Grid", url="https://example.invalid/ingest", bearer_token="token"
                ),
            ),
        )

    def test_rejects_plain_http_url(self) -> None:
        path = self._write(
            "publishing:\n"
            "  enabled: true\n"
            "  http_endpoints:\n"
            "    - url: http://example.invalid/ingest\n"
            "      bearer_token: token\n"
        )

        with self.assertRaises(ConfigError):
            load_config(path)

    def test_rejects_endpoint_without_token(self) -> None:
        path = self._write(
            "publishing:\n"
            "  enabled: true\n"
            "  http_endpoints:\n"
            "    - url: https://example.invalid/ingest\n"
        )

        with self.assertRaises(ConfigError):
            load_config(path)

    def test_enabled_publishing_needs_a_bucket_or_an_endpoint(self) -> None:
        path = self._write("publishing:\n  enabled: true\n")

        with self.assertRaises(ConfigError):
            load_config(path)

    def test_endpoints_alone_satisfy_enabled_publishing(self) -> None:
        path = self._write(
            "publishing:\n"
            "  enabled: true\n"
            "  http_endpoints:\n"
            "    - url: https://example.invalid/ingest\n"
            "      bearer_token: token\n"
        )

        config = load_config(path)

        self.assertIsNone(config.publishing.bucket)
        self.assertEqual(len(config.publishing.http_endpoints), 1)

    def test_disabled_publishing_still_parses_endpoints(self) -> None:
        path = self._write(
            "publishing:\n"
            "  http_endpoints:\n"
            "    - url: https://example.invalid/ingest\n"
            "      bearer_token: token\n"
        )

        config = load_config(path)

        self.assertFalse(config.publishing.enabled)
        self.assertEqual(len(config.publishing.http_endpoints), 1)


class PublishingConfigDefaultsTest(unittest.TestCase):
    def test_defaults_have_no_endpoints(self) -> None:
        self.assertEqual(PublishingConfig().http_endpoints, ())


if __name__ == "__main__":
    unittest.main()
