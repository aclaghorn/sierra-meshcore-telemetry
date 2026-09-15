from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from telemetry.config import PublishingConfig
from telemetry.db import Database
from telemetry.publisher import S3Publisher


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> None:
        self.objects.append(kwargs)


class PublisherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "telemetry.db")
        await self.db.connect()
        await self.db.register_repeater(
            "0123456789abcdef",
            "Hilltop",
            "0123456789abcdef",
            "deadbeef",
        )
        await self.db.record_reading(
            "0123456789abcdef",
            "Hilltop",
            [
                {"type": "voltage", "value": 3.91},
                {"type": "temperature", "value": 18.4},
            ],
            attempt=1,
            ts=1_789_416_000,
        )
        await self.db.record_attempt(
            "0123456789abcdef", "Hilltop", True, 1, "sensitive failure detail", ts=1_789_416_000
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_snapshot_uses_public_id_and_omits_sensitive_fields(self) -> None:
        snapshot = await self.db.public_snapshot(1_789_416_100)
        repeater = snapshot["current"]["repeaters"][0]

        self.assertNotEqual(repeater["id"], "0123456789abcdef")
        self.assertEqual(repeater["name"], "Hilltop")
        self.assertNotIn("public_key", repeater)
        self.assertNotIn("path", repeater)
        self.assertNotIn("error", repeater)

    async def test_publisher_backfills_once_and_gzips_day_files(self) -> None:
        client = FakeS3Client()
        publisher = S3Publisher(
            PublishingConfig(enabled=True, bucket="public-bucket"),
            self.db,
            client,
        )

        await publisher.publish(now=1_789_416_100)
        first_keys = [obj["Key"] for obj in client.objects]
        self.assertEqual(
            first_keys,
            ["data/current.json", "data/summary.json", "data/days/2026-09-14.json.gz"],
        )

        day_object = client.objects[-1]
        self.assertEqual(day_object["ContentEncoding"], "gzip")
        day = json.loads(gzip.decompress(day_object["Body"]))
        self.assertEqual(day["readings"][0]["name"], "Hilltop")
        self.assertNotIn("raw_lpp", day["readings"][0])
        self.assertNotIn("error", day["attempts"][0])

        await publisher.publish(now=1_789_416_200)
        second_keys = [obj["Key"] for obj in client.objects[3:]]
        self.assertEqual(
            second_keys,
            ["data/current.json", "data/summary.json", "data/days/2026-09-14.json.gz"],
        )


if __name__ == "__main__":
    unittest.main()
