"""Service entry point: connect, poll on a schedule, keep going."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from .collector import Collector, DeviceError
from .db import Database
from .publisher import PublicationError, S3Publisher

logger = logging.getLogger("telemetry")

RECONNECT_BACKOFF_SECONDS = (5, 15, 30, 60, 120)


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # The meshcore library logs every frame at INFO; keep it to warnings unless
    # the whole service is in debug mode.
    if logging.getLogger().level > logging.DEBUG:
        logging.getLogger("meshcore").setLevel(logging.WARNING)


def _reload_config(path: Path, current: Config) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        logger.error("Keeping previous configuration, reload failed: %s", exc)
        return current


async def _connect_with_backoff(collector: Collector, stop: asyncio.Event) -> bool:
    attempt = 0
    while not stop.is_set():
        try:
            await collector.connect()
            return True
        except Exception as exc:  # noqa: BLE001 - retry any connection failure
            delay = RECONNECT_BACKOFF_SECONDS[min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            logger.error("Connection failed (%s); retrying in %ds", exc, delay)
            attempt += 1
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
    return False


async def run(config_path: Path) -> None:
    config = load_config(config_path)
    logger.info(
        "Loaded %d repeaters; polling every %.0fs with up to %d attempt(s) each",
        len(config.repeaters),
        config.polling.interval_seconds,
        config.polling.attempts,
    )

    db = Database(config.storage.path)
    await db.connect()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    collector = Collector(config, db)
    publisher: S3Publisher | None = None
    connected = False

    try:
        while not stop.is_set():
            config = _reload_config(config_path, config)
            collector.update_config(config)
            if config.publishing.enabled and (
                publisher is None or publisher.config != config.publishing
            ):
                publisher = S3Publisher(config.publishing, db)
            elif not config.publishing.enabled:
                publisher = None

            if not connected:
                connected = await _connect_with_backoff(collector, stop)
                if not connected:
                    break

            started = time.monotonic()
            try:
                successes, total = await collector.run_cycle()
                logger.info("Cycle complete: %d/%d repeaters reported", successes, total)
            except DeviceError as exc:
                logger.error("Companion radio problem: %s; reconnecting", exc)
                await collector.disconnect()
                connected = False
                continue
            except asyncio.CancelledError:
                raise

            purged = await db.purge(config.storage.retention_days)
            if purged:
                logger.info("Purged %d readings older than %d days", purged, config.storage.retention_days)

            if publisher is not None:
                try:
                    await publisher.publish()
                except PublicationError as exc:
                    logger.error("Telemetry publication failed: %s", exc)

            elapsed = time.monotonic() - started
            sleep_for = max(0.0, config.polling.interval_seconds - elapsed)
            logger.info("Next cycle in %.0fs", sleep_for)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=sleep_for)
    finally:
        logger.info("Shutting down")
        await collector.disconnect()
        await db.close()


async def check(config_path: Path) -> int:
    """Preflight: resolve contacts and show the routes that would be applied."""
    config = load_config(config_path)
    collector = Collector(config, Database(config.storage.path))
    await collector.connect()
    try:
        report = await collector.preflight()
    finally:
        await collector.disconnect()

    missing = 0
    print(
        f"{'repeater':<26} {'contact':<8} {'current path':<16} "
        f"{'config path':<16} {'hops':<5} action"
    )
    for row in report:
        if not row["found"]:
            missing += 1
        config_path_text = "<unchanged>" if row["config_path"] is None else (
            row["config_path"] or "<direct>"
        )
        current = row["current_path"]
        current_text = "-" if current is None else (current or "<direct>")
        action = "set path" if row["changes"] else "ok"
        print(
            f"{row['name']:<26} {'yes' if row['found'] else 'MISSING':<8} "
            f"{current_text:<16} {config_path_text:<16} "
            f"{'' if row['hops'] is None else row['hops']:<5} {action}"
        )

    if missing:
        print(f"\n{missing} repeater(s) are not in the companion's contact list.")
    return 1 if missing else 0


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="mesh-telemetry", description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config against the companion's contacts and exit",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("MESH_CONFIG", DEFAULT_CONFIG_PATH),
        help="path to config.yaml (default: $MESH_CONFIG)",
    )
    args = parser.parse_args()
    config_path = Path(args.config)

    try:
        if args.check:
            raise SystemExit(asyncio.run(check(config_path)))
        asyncio.run(run(config_path))
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
