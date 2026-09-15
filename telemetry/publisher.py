"""Publish telemetry snapshots to S3 and to ingest API endpoints."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import HttpEndpointConfig, PublishingConfig
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


_HEX_ID = re.compile(r"^[0-9a-f]{8,64}$")

# The ingest API's own limits; exceeding them is a 400/413, not something a
# retry fixes.
MAX_REPEATERS_PER_REPORT = 1000
MAX_BODY_BYTES = 1_000_000

# 400/401/403/404/413 all mean "fix the report or the token"; only 429 and 5xx
# are worth sending again.
RETRY_BACKOFF_SECONDS = (2.0, 8.0, 30.0)
MAX_RETRY_AFTER_SECONDS = 120.0


class HttpPublisher:
    """Posts one complete repeater report per cycle to an ingest endpoint."""

    def __init__(
        self,
        endpoints: tuple[HttpEndpointConfig, ...],
        database: Database,
        opener: Any | None = None,
    ) -> None:
        if not endpoints:
            raise ValueError("HttpPublisher requires at least one endpoint")
        self.endpoints = endpoints
        self.database = database
        self._open = opener or urllib.request.urlopen

    async def publish(self, now: int | None = None) -> None:
        stamp = int(now if now is not None else datetime.now(UTC).timestamp())
        try:
            report = await self.database.ingest_report(stamp)
        except sqlite3.Error as exc:
            raise PublicationError(f"failed to export telemetry: {exc}") from exc

        report["repeaters"] = self._valid_repeaters(report["repeaters"])
        body = json.dumps(
            report, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()

        if len(report["repeaters"]) > MAX_REPEATERS_PER_REPORT:
            raise PublicationError(
                f"report holds {len(report['repeaters'])} repeaters, "
                f"more than the {MAX_REPEATERS_PER_REPORT} an ingest report accepts"
            )
        if len(body) > MAX_BODY_BYTES:
            raise PublicationError(
                f"report body is {len(body)} bytes, over the {MAX_BODY_BYTES} byte limit"
            )

        failures: list[str] = []
        for endpoint in self.endpoints:
            try:
                await self._post(endpoint, body)
            except PublicationError as exc:
                failures.append(str(exc))
        if failures:
            raise PublicationError("; ".join(failures))

    @staticmethod
    def _valid_repeaters(repeaters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop entries the ingest API would reject for their id.

        Repeaters configured by name only have no public key to report, and
        sending a non-hex id would just come back as a warning.
        """
        keep = []
        for repeater in repeaters:
            identifier = str(repeater.get("id") or "").lower()
            if _HEX_ID.match(identifier) and len(identifier) % 2 == 0:
                keep.append({**repeater, "id": identifier})
            else:
                logger.warning(
                    "Not reporting '%s': '%s' is not an 8-64 character hex public key",
                    repeater.get("name"),
                    repeater.get("id"),
                )
        return keep

    async def _post(self, endpoint: HttpEndpointConfig, body: bytes) -> None:
        last_error = ""
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            try:
                accepted, warnings = await asyncio.to_thread(
                    self._post_once, endpoint, body
                )
            except _RetryableIngestError as exc:
                last_error = str(exc)
                if attempt == len(RETRY_BACKOFF_SECONDS):
                    break
                delay = exc.retry_after
                if delay is None:
                    delay = RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "%s rejected the report (%s); retrying in %.0fs",
                    endpoint.name,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            except _IngestError as exc:
                raise PublicationError(f"{endpoint.name}: {exc}") from exc

            for warning in warnings:
                logger.warning("%s: %s", endpoint.name, warning)
            logger.info(
                "Published %d repeater(s) to %s", accepted, endpoint.name
            )
            return

        raise PublicationError(f"{endpoint.name}: {last_error}")

    def _post_once(
        self, endpoint: HttpEndpointConfig, body: bytes
    ) -> tuple[int, list[str]]:
        request = urllib.request.Request(
            endpoint.url,
            data=body,
            headers={
                "Authorization": f"Bearer {endpoint.bearer_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._open(request, timeout=endpoint.timeout_seconds) as response:
                payload = json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            if exc.code == 429 or exc.code >= 500:
                raise _RetryableIngestError(
                    f"HTTP {exc.code} {detail}",
                    retry_after=_retry_after(exc),
                ) from exc
            raise _IngestError(f"HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _RetryableIngestError(f"request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise _IngestError(f"unreadable response: {exc}") from exc

        warnings = payload.get("warnings") or []
        return int(payload.get("accepted", 0)), [str(warning) for warning in warnings]


class _IngestError(Exception):
    """An ingest endpoint refused the report."""


class _RetryableIngestError(_IngestError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _http_error_detail(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode(errors="replace").strip()[:500]
    except OSError:
        return error.reason or ""


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    raw = error.headers.get("Retry-After") if error.headers else None
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return None
