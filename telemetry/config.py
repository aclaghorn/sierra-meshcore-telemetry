"""Configuration loading for the MeshCore telemetry collector."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(os.environ.get("MESH_CONFIG", "/config/config.yaml"))

FLOOD = "flood"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class DeviceConfig:
    port: str = "/dev/ttyACM0"
    baudrate: int = 115200
    # Bytes per hop in a route are path_hash_mode + 1. None means "ask the
    # companion for its configured mode".
    path_hash_mode: int | None = None
    # Companion TX power in dBm. None leaves whatever the companion already
    # has. A high-power companion on a Pi can trip the USB port's
    # over-current protection during transmit and disconnect mid-request;
    # lowering this (e.g. to 14) fixes that without touching the repeaters.
    tx_power: int | None = None


@dataclass(frozen=True)
class PollingConfig:
    interval_seconds: float = 900.0
    attempts: int = 3
    retry_delay_seconds: float = 20.0
    stagger_seconds: float = 5.0
    request_timeout_seconds: float = 0.0
    min_request_timeout_seconds: float = 20.0
    always_login: bool = True
    reauthenticate_interval_seconds: float = 43200.0


@dataclass(frozen=True)
class StorageConfig:
    path: str = "/data/telemetry.db"
    retention_days: int = 365


@dataclass(frozen=True)
class PublishingConfig:
    enabled: bool = False
    bucket: str | None = None
    prefix: str = "data"
    region: str | None = None


@dataclass(frozen=True)
class RepeaterConfig:
    name: str
    password: str
    public_key: str | None = None
    path: str | None = None
    path_hash_mode: int | None = None

    @property
    def key(self) -> str:
        """Stable identifier used as the primary key in the database."""
        return (self.public_key or self.name).lower()

    @property
    def hop_count(self) -> int | None:
        """Number of hops the configured path describes, if it is knowable."""
        if self.path is None or self.path in ("", FLOOD) or self.path_hash_mode is None:
            return None
        return len(self.path) // (2 * (self.path_hash_mode + 1))


@dataclass(frozen=True)
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    publishing: PublishingConfig = field(default_factory=PublishingConfig)
    repeaters: tuple[RepeaterConfig, ...] = ()


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' must be a mapping")
    return value


def _positive(value: Any, name: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be a number") from exc
    if number < 0 or (number == 0 and not allow_zero):
        raise ConfigError(f"'{name}' must be greater than {'or equal to 0' if allow_zero else '0'}")
    return number


def _parse_hash_mode(value: Any, name: str) -> int:
    try:
        mode = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be 0 or 1") from exc
    if mode not in (0, 1):
        raise ConfigError(f"'{name}' must be 0 (1 byte per hop) or 1 (2 bytes per hop)")
    return mode


def _parse_repeaters(raw: Any, default_hash_mode: int | None) -> tuple[RepeaterConfig, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("'repeaters' must be a non-empty list")

    repeaters: list[RepeaterConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"repeaters[{index}] must be a mapping")

        public_key = entry.get("public_key")
        name = entry.get("name")
        if not public_key and not name:
            raise ConfigError(f"repeaters[{index}] needs a 'public_key' or a 'name'")
        if public_key is not None:
            public_key = str(public_key).strip().lower()
            if not public_key or any(c not in "0123456789abcdef" for c in public_key):
                raise ConfigError(f"repeaters[{index}] 'public_key' must be a hex string")

        password = entry.get("password")
        if password is None:
            raise ConfigError(f"repeaters[{index}] needs a 'password'")

        hash_mode = entry.get("path_hash_mode", default_hash_mode)
        if hash_mode is not None:
            hash_mode = _parse_hash_mode(hash_mode, f"repeaters[{index}].path_hash_mode")

        path = entry.get("path")
        if path is not None:
            path = str(path).strip().lower().replace(",", "").replace(" ", "")
            if path not in ("", FLOOD):
                if any(c not in "0123456789abcdef" for c in path):
                    raise ConfigError(
                        f"repeaters[{index}] 'path' must be hex hop hashes, "
                        f"an empty string, or '{FLOOD}'"
                    )
                hop_size = 2 * ((hash_mode or 0) + 1)
                if len(path) % hop_size:
                    raise ConfigError(
                        f"repeaters[{index}] 'path' of '{path}' is not a whole number of hops: "
                        f"path_hash_mode {hash_mode or 0} uses {hop_size // 2} byte(s) per hop"
                    )

        repeater = RepeaterConfig(
            name=str(name or public_key),
            password=str(password),
            public_key=public_key,
            path=path,
            path_hash_mode=hash_mode,
        )
        if repeater.key in seen:
            raise ConfigError(f"duplicate repeater '{repeater.name}'")
        seen.add(repeater.key)
        repeaters.append(repeater)

    return tuple(repeaters)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Read, validate and return the configuration."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    device_raw = _section(raw, "device")
    polling_raw = _section(raw, "polling")
    storage_raw = _section(raw, "storage")
    publishing_raw = _section(raw, "publishing")

    device_hash_mode = device_raw.get("path_hash_mode")
    if device_hash_mode is not None:
        device_hash_mode = _parse_hash_mode(device_hash_mode, "device.path_hash_mode")

    device_tx_power = device_raw.get("tx_power")
    if device_tx_power is not None:
        try:
            device_tx_power = int(device_tx_power)
        except (TypeError, ValueError) as exc:
            raise ConfigError("'device.tx_power' must be an integer (dBm)") from exc
        if not 0 <= device_tx_power <= 30:
            raise ConfigError("'device.tx_power' must be between 0 and 30 (dBm)")

    device = DeviceConfig(
        port=str(device_raw.get("port", DeviceConfig.port)),
        baudrate=int(device_raw.get("baudrate", DeviceConfig.baudrate)),
        path_hash_mode=device_hash_mode,
        tx_power=device_tx_power,
    )

    attempts = int(polling_raw.get("attempts", PollingConfig.attempts))
    if attempts < 1:
        raise ConfigError("'polling.attempts' must be at least 1")

    polling = PollingConfig(
        interval_seconds=_positive(
            polling_raw.get("interval_seconds", PollingConfig.interval_seconds),
            "polling.interval_seconds",
        ),
        attempts=attempts,
        retry_delay_seconds=_positive(
            polling_raw.get("retry_delay_seconds", PollingConfig.retry_delay_seconds),
            "polling.retry_delay_seconds",
            allow_zero=True,
        ),
        stagger_seconds=_positive(
            polling_raw.get("stagger_seconds", PollingConfig.stagger_seconds),
            "polling.stagger_seconds",
            allow_zero=True,
        ),
        request_timeout_seconds=_positive(
            polling_raw.get("request_timeout_seconds", PollingConfig.request_timeout_seconds),
            "polling.request_timeout_seconds",
            allow_zero=True,
        ),
        min_request_timeout_seconds=_positive(
            polling_raw.get(
                "min_request_timeout_seconds", PollingConfig.min_request_timeout_seconds
            ),
            "polling.min_request_timeout_seconds",
            allow_zero=True,
        ),
        always_login=bool(polling_raw.get("always_login", PollingConfig.always_login)),
        reauthenticate_interval_seconds=_positive(
            polling_raw.get(
                "reauthenticate_interval_seconds",
                PollingConfig.reauthenticate_interval_seconds,
            ),
            "polling.reauthenticate_interval_seconds",
        ),
    )

    retention_days = int(storage_raw.get("retention_days", StorageConfig.retention_days))
    if retention_days < 0:
        raise ConfigError("'storage.retention_days' must be 0 or greater")

    storage = StorageConfig(
        path=str(storage_raw.get("path", StorageConfig.path)),
        retention_days=retention_days,
    )

    publishing_enabled = bool(publishing_raw.get("enabled", PublishingConfig.enabled))
    publishing_bucket = publishing_raw.get("bucket")
    if publishing_bucket is not None:
        publishing_bucket = str(publishing_bucket).strip()
    if publishing_enabled and not publishing_bucket:
        raise ConfigError("'publishing.bucket' is required when publishing is enabled")

    publishing_prefix = str(
        publishing_raw.get("prefix", PublishingConfig.prefix)
    ).strip("/")
    if not publishing_prefix:
        raise ConfigError("'publishing.prefix' must not be empty")

    publishing_region = publishing_raw.get("region")
    if publishing_region is not None:
        publishing_region = str(publishing_region).strip() or None

    publishing = PublishingConfig(
        enabled=publishing_enabled,
        bucket=publishing_bucket,
        prefix=publishing_prefix,
        region=publishing_region,
    )

    return Config(
        device=device,
        polling=polling,
        storage=storage,
        publishing=publishing,
        repeaters=_parse_repeaters(raw.get("repeaters"), device.path_hash_mode),
    )
