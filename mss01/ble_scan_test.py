"""Console BLE scan test for Windows bleak/WinRT troubleshooting."""

from __future__ import annotations

import asyncio
import sys

try:
    from bleak import BleakScanner
except ImportError:
    print("bleak is not installed. Run: pip install bleak")
    raise SystemExit(1)


SCAN_SECONDS = 30.0


async def main() -> None:
    print("BLE scan start")
    print(f"Python: {sys.version}")
    print(f"Scan duration: {SCAN_SECONDS:.0f}s")

    seen: set[str] = set()
    total_callbacks = 0

    def callback(device, advertisement_data) -> None:
        nonlocal total_callbacks
        total_callbacks += 1
        uuids = [str(uuid) for uuid in (advertisement_data.service_uuids or [])]
        uuids_text = ",".join(uuids) if uuids else "-"
        print(
            "FOUND:",
            f"address={device.address}",
            f"name={device.name!r}",
            f"local_name={advertisement_data.local_name!r}",
            f"rssi={advertisement_data.rssi}",
            f"uuids={uuids_text}",
            flush=True,
        )
        seen.add(device.address)

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(SCAN_SECONDS)
    await scanner.stop()

    print("BLE scan done")
    print(f"callbacks={total_callbacks}, unique_devices={len(seen)}")
    if not seen:
        print("No BLE devices detected.")
        print("Check: Windows Bluetooth ON, bleak winrt packages, location permission.")


if __name__ == "__main__":
    asyncio.run(main())
