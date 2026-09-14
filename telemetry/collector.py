"""Polls MeshCore repeaters for battery and environment telemetry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from meshcore import EventType, MeshCore

from .config import Config, RepeaterConfig
from .db import Database

logger = logging.getLogger(__name__)

FLOOD = "flood"


class TelemetryError(Exception):
    """A single telemetry attempt failed for a recoverable reason."""


class AuthError(TelemetryError):
    """The repeater rejected the password; retrying will not help."""


class DeviceError(Exception):
    """The companion radio itself is unusable; the connection must be rebuilt."""


class Collector:
    """Owns the serial link to the companion radio and the polling loop."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self._mc: MeshCore | None = None
        self._device_hash_mode: int | None = None

    def update_config(self, config: Config) -> None:
        self.config = config

    async def connect(self) -> None:
        device = self.config.device
        logger.info("Connecting to companion radio on %s", device.port)
        mc = await MeshCore.create_serial(
            port=device.port,
            baudrate=device.baudrate,
            auto_reconnect=True,
            max_reconnect_attempts=5,
        )
        if mc is None:
            raise DeviceError(f"no response from MeshCore companion on {device.port}")

        self._mc = mc
        await mc.ensure_contacts()
        await self._apply_tx_power()
        self._device_hash_mode = await self._query_device_hash_mode()
        info = mc.self_info or {}
        logger.info(
            "Connected to '%s' with %d contacts (tx_power=%s dBm, "
            "device path_hash_mode=%s, %d byte(s) per hop)",
            info.get("name", "unknown"),
            len(mc.contacts or {}),
            info.get("tx_power"),
            self._device_hash_mode,
            (self._device_hash_mode or 0) + 1,
        )

    async def _apply_tx_power(self) -> None:
        """Set the companion's TX power if configured.

        A high companion TX power can spike current enough to trip a Pi
        USB port's over-current protection during transmit, dropping the
        serial link mid-request. This is applied on every connect (and
        every reconnect) so it survives companion reboots/reflashes.
        """
        wanted = self.config.device.tx_power
        if wanted is None:
            return

        mc = self._require_mc()
        current = (mc.self_info or {}).get("tx_power")
        if current == wanted:
            return

        logger.info("Setting companion TX power to %d dBm (was %s)", wanted, current)
        result = await mc.commands.set_tx_power(wanted)
        if result is None or result.type == EventType.ERROR:
            raise DeviceError(f"failed to set companion TX power to {wanted} dBm")

        # self_info is only refreshed by an appstart round-trip.
        await mc.commands.send_appstart()

    async def _query_device_hash_mode(self) -> int:
        """Bytes per hop is path_hash_mode + 1; routes must match the network."""
        if self.config.device.path_hash_mode is not None:
            return self.config.device.path_hash_mode
        result = await self._require_mc().commands.send_device_query()
        if result is None or result.type == EventType.ERROR:
            return 0
        return int(result.payload.get("path_hash_mode", 0) or 0)

    def hash_mode_for(self, repeater: RepeaterConfig) -> int:
        if repeater.path_hash_mode is not None:
            return repeater.path_hash_mode
        return self._device_hash_mode or 0

    async def disconnect(self) -> None:
        if self._mc is not None:
            mc, self._mc = self._mc, None
            try:
                await mc.disconnect()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                logger.debug("Error while disconnecting", exc_info=True)

    async def _refresh_contacts(self) -> None:
        mc = self._require_mc()
        try:
            await mc.commands.get_contacts()
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"unable to refresh contacts: {exc}") from exc

    def _require_mc(self) -> MeshCore:
        if self._mc is None:
            raise DeviceError("not connected to a companion radio")
        return self._mc

    def _find_contact(self, repeater: RepeaterConfig) -> dict[str, Any] | None:
        mc = self._require_mc()
        if repeater.public_key:
            contact = mc.get_contact_by_key_prefix(repeater.public_key)
            if contact is not None:
                return contact
        return mc.get_contact_by_name(repeater.name)

    async def _apply_path(self, contact: dict[str, Any], repeater: RepeaterConfig) -> None:
        """Force the contact onto the hard coded route from the config."""
        if repeater.path is None:
            return

        mc = self._require_mc()
        wanted_flood = repeater.path == FLOOD
        # out_path_len of -1 is how the firmware reports "flood routing".
        is_flood = contact.get("out_path_len", 0) == -1
        current = (contact.get("out_path") or "").lower()
        current_mode = contact.get("out_path_hash_mode", 0)
        wanted = "" if wanted_flood else repeater.path
        wanted_mode = self.hash_mode_for(repeater)

        if wanted_flood:
            if is_flood:
                return
            logger.info("%s: resetting to flood routing", repeater.name)
            result = await mc.commands.reset_path(contact)
        else:
            # The hash mode is meaningless for a zero-hop (direct) route, so
            # only let it force a rewrite when there are actual hops.
            mode_matches = not wanted or current_mode == wanted_mode
            if not is_flood and current == wanted and mode_matches:
                return
            hops = len(wanted) // (2 * (wanted_mode + 1))
            logger.info(
                "%s: setting path to '%s' (%d hop(s), %d byte(s) per hop)",
                repeater.name,
                wanted or "<direct>",
                hops,
                wanted_mode + 1,
            )
            result = await mc.commands.change_contact_path(
                contact, wanted, path_hash_mode=wanted_mode
            )

        if result is None or result.type == EventType.ERROR:
            raise TelemetryError(f"failed to set path for {repeater.name}")

        await self._refresh_contacts()

    async def _login(self, contact: dict[str, Any], repeater: RepeaterConfig) -> None:
        """Log in, distinguishing a rejected password from a dead link.

        The library's send_login_sync only waits for LOGIN_SUCCESS, so a wrong
        password, a lost USB link and a silent repeater all surface as the same
        timeout. Watching LOGIN_FAILED and DISCONNECTED alongside it turns
        those into distinct, actionable errors.
        """
        mc = self._require_mc()
        outcome: dict[str, Any] = {}

        def on_failed(event: Any) -> None:
            outcome.setdefault("rejected", event)

        def on_disconnect(event: Any) -> None:
            outcome.setdefault("disconnected", event)

        subs = [
            mc.subscribe(EventType.LOGIN_FAILED, on_failed),
            mc.subscribe(EventType.DISCONNECTED, on_disconnect),
        ]
        try:
            result = await mc.commands.send_login_sync(
                contact,
                repeater.password,
                timeout=self.config.polling.request_timeout_seconds,
                min_timeout=self.config.polling.min_request_timeout_seconds,
            )
        finally:
            for sub in subs:
                with contextlib.suppress(Exception):
                    mc.unsubscribe(sub)

        if "disconnected" in outcome:
            reason = (getattr(outcome["disconnected"], "payload", {}) or {}).get(
                "reason", "unknown"
            )
            raise DeviceError(
                f"companion radio disconnected while logging in to {repeater.name} "
                f"(reason: {reason}). This is usually USB power: a LoRa transmit "
                f"spike can trip the port's over-current limit. Check "
                f"`journalctl -k | grep over-current`."
            )
        if "rejected" in outcome:
            raise AuthError(f"{repeater.name} rejected the password")
        if result is None or result.type == EventType.ERROR:
            raise TelemetryError(
                f"no login response from {repeater.name} "
                f"(waited {self.config.polling.min_request_timeout_seconds:.0f}s)"
            )
        logger.debug("%s: login ok", repeater.name)

    async def _request_telemetry(
        self, contact: dict[str, Any], repeater: RepeaterConfig
    ) -> list[dict[str, Any]]:
        mc = self._require_mc()
        lpp = await mc.commands.req_telemetry_sync(
            contact,
            timeout=self.config.polling.request_timeout_seconds,
            min_timeout=self.config.polling.min_request_timeout_seconds,
        )
        if not lpp:
            raise TelemetryError(f"no telemetry response from {repeater.name}")
        return lpp

    async def _request_status(
        self, contact: dict[str, Any], repeater: RepeaterConfig
    ) -> dict[str, Any] | None:
        """Fetch uptime/airtime/packet-count/noise-floor stats.

        Best-effort: a failure here is logged and returns None rather than
        raising, so a repeater that only answers telemetry (or vice versa)
        still counts as a successful poll.
        """
        mc = self._require_mc()
        try:
            status = await mc.commands.req_status_sync(
                contact,
                timeout=self.config.polling.request_timeout_seconds,
                min_timeout=self.config.polling.min_request_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - never let stats break the poll
            logger.warning("%s: stats request raised: %s", repeater.name, exc)
            return None
        if not status:
            logger.warning("%s: no stats response", repeater.name)
            return None
        return status

    async def _poll_once(
        self, repeater: RepeaterConfig
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        contact = self._find_contact(repeater)
        if contact is None:
            await self._refresh_contacts()
            contact = self._find_contact(repeater)
        if contact is None:
            raise TelemetryError(
                f"{repeater.name} is not in the companion's contact list "
                f"(public_key={repeater.public_key})"
            )

        await self._apply_path(contact, repeater)
        if self.config.polling.always_login:
            await self._login(contact, repeater)
        lpp = await self._request_telemetry(contact, repeater)
        status = await self._request_status(contact, repeater)
        return lpp, status

    async def preflight(self) -> list[dict[str, Any]]:
        """Resolve every configured repeater and report the planned routing.

        Read-only: it never writes to the companion and never touches the mesh.
        """
        await self._refresh_contacts()
        report: list[dict[str, Any]] = []

        for repeater in self.config.repeaters:
            contact = self._find_contact(repeater)
            mode = self.hash_mode_for(repeater)
            row: dict[str, Any] = {
                "name": repeater.name,
                "found": contact is not None,
                "config_path": repeater.path,
                "hash_mode": mode,
                "hops": None,
                "current_path": None,
                "changes": False,
            }
            if contact is not None:
                is_flood = contact.get("out_path_len", 0) == -1
                row["current_path"] = FLOOD if is_flood else (contact.get("out_path") or "")
                row["current_hash_mode"] = contact.get("out_path_hash_mode")
                if repeater.path is not None and repeater.path != FLOOD:
                    row["hops"] = len(repeater.path) // (2 * (mode + 1))
                if repeater.path is None:
                    row["changes"] = False
                elif repeater.path == FLOOD:
                    row["changes"] = not is_flood
                else:
                    row["changes"] = (
                        is_flood
                        or row["current_path"] != repeater.path
                        or (
                            bool(repeater.path)
                            and contact.get("out_path_hash_mode") != mode
                        )
                    )
            report.append(row)

        return report

    async def poll_repeater(self, repeater: RepeaterConfig) -> bool:
        """Try a repeater up to `attempts` times, storing the first success."""
        polling = self.config.polling
        last_error = "unknown error"

        for attempt in range(1, polling.attempts + 1):
            try:
                lpp, status = await self._poll_once(repeater)
            except AuthError as exc:
                # A bad password will not fix itself; stop burning airtime.
                last_error = str(exc)
                logger.error("%s: %s; skipping until the config changes", repeater.name, exc)
                await self.db.record_attempt(
                    repeater.key, repeater.name, False, attempt, last_error
                )
                return False
            except TelemetryError as exc:
                last_error = str(exc)
                logger.warning(
                    "%s: attempt %d/%d failed: %s",
                    repeater.name,
                    attempt,
                    polling.attempts,
                    last_error,
                )
            except (DeviceError, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - one bad repeater must not kill the loop
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "%s: attempt %d/%d raised: %s",
                    repeater.name,
                    attempt,
                    polling.attempts,
                    last_error,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                if not self._require_mc().is_connected:
                    raise DeviceError("companion radio disconnected") from exc
            else:
                values = await self.db.record_reading(
                    repeater.key, repeater.name, lpp, attempt
                )
                if status is not None:
                    await self.db.record_stats(repeater.key, repeater.name, status)
                logger.info(
                    "%s: battery=%sV (%s%%) temp=%s°C uptime=%ss noise=%sdBm "
                    "after %d attempt(s)",
                    repeater.name,
                    values["battery_voltage"],
                    values["battery_percent"],
                    values["temperature_c"],
                    (status or {}).get("uptime"),
                    (status or {}).get("noise_floor"),
                    attempt,
                )
                await self.db.record_attempt(repeater.key, repeater.name, True, attempt, None)
                return True

            if attempt < polling.attempts and polling.retry_delay_seconds:
                await asyncio.sleep(polling.retry_delay_seconds)

        logger.error(
            "%s: giving up after %d attempt(s): %s", repeater.name, polling.attempts, last_error
        )
        await self.db.record_attempt(
            repeater.key, repeater.name, False, polling.attempts, last_error
        )
        return False

    async def run_cycle(self) -> tuple[int, int]:
        """Poll every configured repeater once. Returns (successes, total)."""
        repeaters = self.config.repeaters
        successes = 0

        for index, repeater in enumerate(repeaters):
            await self.db.register_repeater(
                repeater.key, repeater.name, repeater.public_key, repeater.path
            )
            if await self.poll_repeater(repeater):
                successes += 1
            if index < len(repeaters) - 1 and self.config.polling.stagger_seconds:
                await asyncio.sleep(self.config.polling.stagger_seconds)

        return successes, len(repeaters)
