"""Add missing configured repeaters to the companion's contacts."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from meshcore import EventType, MeshCore
from meshcore.packets import AdvType

from telemetry.config import load_config


async def add_contacts(config_path: Path) -> None:
    config = load_config(config_path)
    for repeater in config.repeaters:
        if not repeater.public_key or len(repeater.public_key) != 64:
            raise ValueError(f"{repeater.name}: full public key required")

    mc = await MeshCore.create_serial(
        port=config.device.port,
        baudrate=config.device.baudrate,
    )
    if mc is None:
        raise RuntimeError("Unable to connect to companion")

    try:
        await mc.ensure_contacts()
        for repeater in config.repeaters:
            if mc.get_contact_by_key_prefix(repeater.public_key) is not None:
                print(f"Already present: {repeater.name}")
                continue

            contact = dict(
                public_key=repeater.public_key,
                adv_name=repeater.name,
                type=AdvType.REPEATER.value,
                flags=0,
                out_path="",
                out_path_len=-1,
                out_path_hash_mode=0,
                last_advert=0,
                adv_lat=0,
                adv_lon=0,
            )
            result = await mc.commands.add_contact(contact)
            if result is None or result.type != EventType.OK:
                raise RuntimeError(f"Failed to add {repeater.name}: {result}")
            print(f"Added: {repeater.name}")
    finally:
        await mc.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "config.yaml",
        help="Configuration file (default: repository config/config.yaml)",
    )
    args = parser.parse_args()
    asyncio.run(add_contacts(args.config))


if __name__ == "__main__":
    main()