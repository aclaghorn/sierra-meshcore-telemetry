"""Publish sanitized telemetry snapshots to S3."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import PublishingConfig
from .db import Database

logger = logging.getLogger(__name__)


class PublicationError(Exception):
    """Raised when telemetry could not be published."""


class S3Publisher:
    def __init__(
        self,
        config: PublishingConfig,
        database: Database,
        client: Any | None = None,
    ) -> None:
        if not config.enabled or not config.bucket:
            raise ValueError("S3Publisher requires enabled publishing with a bucket")
        self.config = config
        self.database = database
        self._client = client or boto3.client("s3", region_name=config.region)
        self._backfilled = False

    def _key(self, name: str) -> str:
        return f"{self.config.prefix}/{name}"

    async def _put_json(
        self,
        name: str,
        document: dict[str, Any],
        *,
        gzip_encoded: bool = False,
        immutable: bool = False,
    ) -> None:
        body = json.dumps(
            document, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
        parameters: dict[str, Any] = {
            "Bucket": self.config.bucket,
            "Key": self._key(name),
            "Body": gzip.compress(body, mtime=0) if gzip_encoded else body,
            "ContentType": "application/json",
            "CacheControl": (
                "public, max-age=31536000, immutable"
                if immutable
                else "public, max-age=300"
            ),
        }
        if gzip_encoded:
            parameters["ContentEncoding"] = "gzip"
        try:
            await asyncio.to_thread(self._client.put_object, **parameters)
        except (BotoCoreError, ClientError) as exc:
            raise PublicationError(
                f"failed to upload s3://{self.config.bucket}/{self._key(name)}: {exc}"
            ) from exc

    async def publish(self, now: int | None = None) -> None:
        stamp = int(now if now is not None else datetime.now(UTC).timestamp())
        try:
            snapshot = await self.database.public_snapshot(stamp)
        except sqlite3.Error as exc:
            raise PublicationError(f"failed to export telemetry: {exc}") from exc
        today = datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d")

        await self._put_json("current.json", snapshot["current"])
        await self._put_json("summary.json", snapshot["summary"])

        available_days = [row["day"] for row in snapshot["summary"]["days"]]
        days = available_days if not self._backfilled else [today]
        for day in days:
            try:
                document = await self.database.public_day(day)
            except (sqlite3.Error, ValueError) as exc:
                raise PublicationError(f"failed to export telemetry for {day}: {exc}") from exc
            await self._put_json(
                f"days/{day}.json.gz",
                document,
                gzip_encoded=True,
                immutable=day != today,
            )

        self._backfilled = True
        logger.info(
            "Published current telemetry and %d day file(s) to s3://%s/%s/",
            len(days),
            self.config.bucket,
            self.config.prefix,
        )
