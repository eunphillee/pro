"""BLE alarm configuration tab for MSS03_ALARM devices."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot, QMetaObject, Q_ARG
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - runtime dependency
    BleakClient = None
    BleakScanner = None


BLE_DEVICE_NAME = "MSS03_ALARM"
BLE_SVC_UUID = "0000a100-0000-1000-8000-00805f9b34fb"
BLE_CHAR_READ_UUID = "0000a101-0000-1000-8000-00805f9b34fb"
BLE_CHAR_WRITE_UUID = "0000a102-0000-1000-8000-00805f9b34fb"
BLE_CHAR_STATUS_UUID = "0000a103-0000-1000-8000-00805f9b34fb"
BLE_WRITE_USE_RESPONSE = False
BLE_STATUS_NOTIFY_TIMEOUT_SEC = 5.0
BLE_READ_DELAY_SEC = 0.8
BLE_READ_AUTO_RECONNECT = True
WINERROR_BLE_CANCELLED = -2147023673
BLE_SCAN_TIMEOUT_SEC = 30.0
BLE_RESCAN_TIMEOUT_SEC = 10.0
BLE_CONNECT_TIMEOUT_SEC = 40.0

MSG_MAX_BYTES = 80
PHONE_MAX_LEN = 12
PHONE_COUNT = 10
MSG_COUNT = 7

MSG_LABELS = [
    "1. 입력 1",
    "2. 입력 2",
    "3. 입력 3",
    "4. 입력 1 + 입력 2",
    "5. 입력 2 + 입력 3",
    "6. 입력 1 + 입력 3",
    "7. 입력 1 + 입력 2 + 입력 3",
]

DEFAULT_MESSAGES = [
    "침수위험(신천IC 배수펌프 #4,5)",
    "대피하세요(신천IC 배수펌프 #4,5)",
    "가스 이상 감지(O2)",
    "가스 이상 감지(CO)",
    "가스 이상 감지(H2S)",
    "가스 이상 감지(LEL)",
    "가스 이상 감지(CO2)",
]

DEFAULT_PHONES = ["01026844484"] + [""] * (PHONE_COUNT - 1)


@dataclass
class BleDeviceInfo:
    address: str
    name: str
    local_name: str
    rssi: int | None
    service_uuids: list[str]

    @property
    def display_label(self) -> str:
        shown = self.local_name or self.name or "(이름 없음)"
        rssi_text = f", {self.rssi} dBm" if self.rssi is not None else ""
        return f"{shown} ({self.address}{rssi_text})"

    @property
    def is_target(self) -> bool:
        if self.name == BLE_DEVICE_NAME or self.local_name == BLE_DEVICE_NAME:
            return True
        return BLE_DEVICE_NAME in self.name or BLE_DEVICE_NAME in self.local_name


def _build_device_info_from_adv(device, advertisement_data) -> BleDeviceInfo:
    service_uuids = [str(uuid) for uuid in (advertisement_data.service_uuids or [])]
    return BleDeviceInfo(
        address=device.address,
        name=device.name or "",
        local_name=advertisement_data.local_name or "",
        rssi=advertisement_data.rssi,
        service_uuids=service_uuids,
    )


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[:PHONE_MAX_LEN]


def message_byte_length(text: str) -> int:
    return len((text or "").encode("utf-8"))


def build_config_dict(messages: list[str], phones: list[str]) -> dict:
    return {
        "messages": messages[:MSG_COUNT],
        "phones": phones[:PHONE_COUNT],
    }


def build_short_test_config() -> dict:
    """Minimal valid config (7 messages + 10 phones) for first write test."""
    messages = ["테스트1"] + [""] * (MSG_COUNT - 1)
    phones = ["01026844484"] + [""] * (PHONE_COUNT - 1)
    return build_config_dict(messages, phones)


def normalize_ble_uuid(uuid: str) -> str:
    return uuid.lower().replace("-", "")


def find_gatt_characteristic(client: BleakClient, target_uuid: str):
    """Find a GATT characteristic by UUID from discovered services."""
    target = normalize_ble_uuid(target_uuid)
    for service in client.services:
        for char in service.characteristics:
            if normalize_ble_uuid(str(char.uuid)) == target:
                return char
    return None


class BleAsyncWorker(QObject):
    """Runs bleak coroutines on a dedicated QThread with its own asyncio loop."""

    log = Signal(str)
    status_changed = Signal(str)
    devices_found = Signal(list)
    config_loaded = Signal(dict)
    save_finished = Signal(bool, str)
    connected_changed = Signal(bool)
    connect_finished = Signal(bool)
    read_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._client: BleakClient | None = None
        self._address: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # address → raw BLEDevice object (for Windows BleakClient(device) style)
        self._device_cache: dict[str, object] = {}
        self._read_char = None
        self._write_char = None
        self._status_char = None
        self._current_save_step = ""
        self._read_in_progress = False
        self._last_connect_address: str | None = None
        self._last_connect_display_name: str | None = None

    def _clear_gatt_cache(self) -> None:
        self._read_char = None
        self._write_char = None
        self._status_char = None

    def _cache_gatt_characteristics(self, client: BleakClient) -> None:
        self._read_char = find_gatt_characteristic(client, BLE_CHAR_READ_UUID)
        self._write_char = find_gatt_characteristic(client, BLE_CHAR_WRITE_UUID)
        self._status_char = find_gatt_characteristic(client, BLE_CHAR_STATUS_UUID)

        for label, char, expected_uuid in (
            ("read", self._read_char, BLE_CHAR_READ_UUID),
            ("write", self._write_char, BLE_CHAR_WRITE_UUID),
            ("status", self._status_char, BLE_CHAR_STATUS_UUID),
        ):
            if char is None:
                self.log.emit(f"[BLE] WARNING: config {label} characteristic not found: {expected_uuid}")
                continue
            props = ",".join(char.properties)
            handle = getattr(char, "handle", None)
            handle_text = f", handle={handle}" if handle is not None else ""
            self.log.emit(f"[BLE] config {label} characteristic: {char.uuid} [{props}]{handle_text}")

    def _ensure_write_characteristic(self, client: BleakClient):
        if self._write_char is None:
            self._write_char = find_gatt_characteristic(client, BLE_CHAR_WRITE_UUID)
        if self._write_char is None:
            raise ValueError(f"write characteristic not found: {BLE_CHAR_WRITE_UUID}")
        props = set(self._write_char.properties)
        if "write" not in props and "write-without-response" not in props:
            raise ValueError(
                f"characteristic {self._write_char.uuid} is not writable: {sorted(props)}"
            )
        return self._write_char

    def _ensure_status_characteristic(self, client: BleakClient):
        if self._status_char is None:
            self._status_char = find_gatt_characteristic(client, BLE_CHAR_STATUS_UUID)
        if self._status_char is None:
            raise ValueError(f"status characteristic not found: {BLE_CHAR_STATUS_UUID}")
        return self._status_char

    async def _write_gatt_payload(
        self,
        client: BleakClient,
        payload: bytes,
        label: str,
    ) -> None:
        await self._write_gatt_payload_with_response(
            client, payload, label, response=BLE_WRITE_USE_RESPONSE
        )

    async def _write_gatt_payload_with_response(
        self,
        client: BleakClient,
        payload: bytes,
        label: str,
        *,
        response: bool,
    ) -> None:
        if not client.is_connected:
            raise ConnectionError("BLE client disconnected before write")

        write_char = self._ensure_write_characteristic(client)
        handle = getattr(write_char, "handle", None)
        self.log.emit(f"[BLE] write target uuid: {write_char.uuid}")
        if handle is not None:
            self.log.emit(f"[BLE] write target handle: {handle}")
        self.log.emit(f"[BLE] write data length: {len(payload)} bytes ({label})")
        self.log.emit(f"[BLE] write response={response}")

        if not client.is_connected:
            raise ConnectionError("BLE client disconnected immediately before write")

        await client.write_gatt_char(write_char, payload, response=response)
        self.log.emit(f"[BLE] write ok ({label})")

    async def _write_and_wait_status_notify(
        self,
        client: BleakClient,
        payload: bytes,
        label: str,
        status_queue: asyncio.Queue[str],
    ) -> str:
        if not client.is_connected:
            raise ConnectionError(f"BLE client disconnected before {label} write")

        await self._write_gatt_payload(client, payload, label)

        if not client.is_connected:
            raise ConnectionError(f"BLE client disconnected after {label} write")

        status = await asyncio.wait_for(
            status_queue.get(),
            timeout=BLE_STATUS_NOTIFY_TIMEOUT_SEC,
        )
        self.log.emit(f"[BLE] status notify result ({label}): {status}")
        return status

    def _format_save_error(self, exc: Exception, step: str) -> str:
        winerror = getattr(exc, "winerror", None)
        if winerror == -2147023673:
            return f"Windows BLE 작업 취소 (step={step}, 연결 끊김 또는 stack abort)"
        if isinstance(exc, ConnectionError):
            return f"BLE 연결 끊김 (step={step})"
        if isinstance(exc, ValueError):
            return f"characteristic UUID/권한 오류 (step={step}): {exc}"
        if isinstance(exc, TimeoutError):
            return f"write timeout (step={step})"
        message = str(exc).lower()
        if "length" in message or "size" in message or "mtu" in message:
            return f"데이터 길이/MTU 문제 (step={step}): {exc}"
        return f"[{type(exc).__name__}] step={step}: {exc}"

    def _parse_read_payload(self, text: str) -> dict | None:
        """Return None for READ_OK test response; dict for config JSON."""
        stripped = text.strip()
        if stripped == "READ_OK":
            return None
        if stripped.startswith("{") or stripped.startswith("["):
            config = json.loads(stripped)
            if not isinstance(config, dict):
                raise ValueError("read response JSON is not an object")
            return config
        raise ValueError(f"unexpected read response: {stripped!r}")

    def _is_ble_cancelled_error(self, exc: Exception) -> bool:
        return getattr(exc, "winerror", None) == WINERROR_BLE_CANCELLED

    async def _reconnect_last_device(self) -> None:
        address = self._last_connect_address
        if not address:
            raise ConnectionError("재연결할 이전 주소 없음")

        self.log.emit(f"[BLE] read 재연결 시도: {address}")
        device_obj = self._device_cache.get(address)
        if device_obj is not None:
            client = BleakClient(
                device_obj,
                disconnected_callback=self._on_ble_disconnected,
                timeout=BLE_CONNECT_TIMEOUT_SEC,
            )
        else:
            client = BleakClient(
                address,
                disconnected_callback=self._on_ble_disconnected,
                timeout=BLE_CONNECT_TIMEOUT_SEC,
            )

        await client.connect()
        if not client.is_connected:
            raise ConnectionError("재연결 후 is_connected=False")

        self._client = client
        self._address = address
        self._cache_gatt_characteristics(client)
        self.log.emit("[BLE] read 재연결 성공")
        self.connected_changed.emit(True)
        self.status_changed.emit(f"연결됨: {self._last_connect_display_name or address}")

    async def _perform_read_once(self) -> dict:
        assert self._client is not None
        client = self._client

        if not client.is_connected:
            raise ConnectionError("BLE client disconnected before read")

        self.log.emit(f"[BLE] read delay {BLE_READ_DELAY_SEC:.1f}s...")
        await asyncio.sleep(BLE_READ_DELAY_SEC)

        if not client.is_connected:
            raise ConnectionError("BLE client disconnected after read delay")

        read_char = self._read_char or find_gatt_characteristic(client, BLE_CHAR_READ_UUID)
        if read_char is None:
            raise ValueError(f"read characteristic not found: {BLE_CHAR_READ_UUID}")

        handle = getattr(read_char, "handle", None)
        self.log.emit(f"[BLE] read target uuid: {read_char.uuid}")
        if handle is not None:
            self.log.emit(f"[BLE] read target handle: {handle}")

        if not client.is_connected:
            raise ConnectionError("BLE client disconnected before read_gatt_char")

        data = await client.read_gatt_char(read_char)
        text = data.decode("utf-8", errors="replace")
        self.log.emit(f"[BLE] read response length: {len(data)} bytes")
        self.log.emit(f"[BLE] read response preview: {text[:80]}")
        return self._parse_read_payload(text)

    @Slot()
    def init_loop(self) -> None:
        """Create asyncio event loop bound to the BLE worker thread."""
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.log.emit("BLE worker asyncio loop ready")

    def _run_async(self, coro):
        if self._loop is None:
            raise RuntimeError("BLE asyncio loop is not ready")
        return self._loop.run_until_complete(coro)

    @Slot()
    def scan_devices(self) -> None:
        if BleakScanner is None:
            self.log.emit("bleak 패키지가 설치되지 않았습니다. pip install bleak")
            self.status_changed.emit("bleak 미설치")
            return
        if self._loop is None:
            self.log.emit("BLE worker loop 미준비 — 잠시 후 다시 시도하세요")
            self.status_changed.emit("loop 미준비")
            return

        self.status_changed.emit("검색 중")
        self.log.emit(f"장치 검색 시작 (callback scan, timeout={BLE_SCAN_TIMEOUT_SEC:.0f}s)")

        async def _scan_async() -> list[BleDeviceInfo]:
            devices_map: dict[str, BleDeviceInfo] = {}
            callback_count = 0

            def detection_callback(device, advertisement_data) -> None:
                nonlocal callback_count
                callback_count += 1
                try:
                    info = _build_device_info_from_adv(device, advertisement_data)
                    devices_map[info.address] = info
                    # keep the raw BLEDevice object so connect_device can use it
                    self._device_cache[device.address] = device
                    uuids_text = ",".join(info.service_uuids) if info.service_uuids else "-"
                    self.log.emit(
                        "[BLE_SCAN] "
                        f"address={info.address}, "
                        f"name={info.name or '-'}, "
                        f"local_name={info.local_name or '-'}, "
                        f"rssi={info.rssi if info.rssi is not None else '-'}, "
                        f"uuids={uuids_text}"
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.emit(f"[BLE_SCAN] callback error: {exc}")

            scanner = BleakScanner(detection_callback=detection_callback)
            await scanner.start()
            self.log.emit("[BLE_SCAN] scanner started")
            await asyncio.sleep(BLE_SCAN_TIMEOUT_SEC)
            await scanner.stop()
            self.log.emit(f"[BLE_SCAN] scanner stopped (callbacks={callback_count})")

            found = list(devices_map.values())
            found.sort(key=lambda item: (-(item.rssi or -999), item.address))
            return found

        try:
            found = self._run_async(_scan_async())
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"검색 실패: {exc}")
            self.status_changed.emit("연결 안 됨")
            return

        self.devices_found.emit(found)
        target_count = sum(1 for item in found if item.is_target)
        if not found:
            self.log.emit("검색된 BLE 장치가 없습니다 (Windows Bluetooth/권한/bleak winrt 확인)")
            self.status_changed.emit("연결 안 됨")
        else:
            self.log.emit(f"검색 완료: 전체 {len(found)}개")
            if target_count:
                self.log.emit(f"MSS03_ALARM {target_count}개 포함")
            self.status_changed.emit(f"검색 완료 ({len(found)}개)")

    @Slot(str, str)
    def connect_device(self, address: str, display_name: str) -> None:
        if BleakClient is None or not address or self._loop is None:
            self.connect_finished.emit(False)
            return

        async def _get_fresh_device(target_address: str) -> object | None:
            """Rescan briefly to get a fresh BLEDevice object (avoids Windows cache miss)."""
            found: list[object] = []
            target_addr_upper = target_address.upper()

            def _cb(device, _adv) -> None:
                if device.address.upper() == target_addr_upper:
                    found.append(device)
                    # also refresh cache
                    self._device_cache[device.address] = device

            scanner = BleakScanner(detection_callback=_cb)
            await scanner.start()
            self.log.emit(f"재스캔 중... ({BLE_RESCAN_TIMEOUT_SEC:.0f}s, address={target_address})")
            deadline = BLE_RESCAN_TIMEOUT_SEC
            elapsed = 0.0
            while elapsed < deadline:
                await asyncio.sleep(0.5)
                elapsed += 0.5
                if found:
                    break
            await scanner.stop()
            return found[0] if found else None

        async def _connect_and_discover() -> None:
            step = "BleakClient 생성"
            try:
                # Prefer cached device object; fall back to fresh rescan
                device_obj = self._device_cache.get(address)

                if device_obj is None:
                    self.log.emit("캐시에 없음, 재스캔 시작")
                    device_obj = await _get_fresh_device(address)

                if device_obj is not None:
                    self.log.emit(f"BLEDevice 객체 사용: {getattr(device_obj, 'address', address)}")
                    client = BleakClient(
                        device_obj,
                        disconnected_callback=self._on_ble_disconnected,
                        timeout=BLE_CONNECT_TIMEOUT_SEC,
                    )
                else:
                    self.log.emit("재스캔에서도 못 찾음 — 주소 문자열로 시도")
                    client = BleakClient(
                        address,
                        disconnected_callback=self._on_ble_disconnected,
                        timeout=BLE_CONNECT_TIMEOUT_SEC,
                    )

                step = "client.connect()"
                self.log.emit(f"await client.connect() 호출 중... (timeout={BLE_CONNECT_TIMEOUT_SEC:.0f}s)")
                await client.connect()

                step = "is_connected 확인"
                if not client.is_connected:
                    raise ConnectionError("BLE client is not connected")

                self._client = client
                self._address = address

                self.log.emit("[BLE] 연결 성공")
                self.log.emit(f"[BLE] is_connected={client.is_connected}")

                step = "service discovery (client.services)"
                self.log.emit("[BLE] service discovery 중...")
                services = client.services
                svc_count = 0
                chr_count = 0
                for service in services:
                    svc_count += 1
                    self.log.emit(f"[BLE] service discovered: {service.uuid}")
                    for char in service.characteristics:
                        chr_count += 1
                        props = ",".join(char.properties)
                        self.log.emit(f"[BLE]   characteristic: {char.uuid} [{props}]")
                self.log.emit(f"[BLE] service discovery 완료: service={svc_count}, characteristic={chr_count}")
                self._cache_gatt_characteristics(client)
            except Exception as exc:  # noqa: BLE001
                self.log.emit(f"연결 실패: [{type(exc).__name__}] step={step}: {exc}")
                raise

        connected = False
        try:
            self._run_async(_connect_and_discover())
            connected = True
            self._last_connect_address = address
            self._last_connect_display_name = display_name
            self.status_changed.emit(f"연결됨: {display_name or address}")
            self.connected_changed.emit(True)
        except TimeoutError:
            self._client = None
            self._address = None
            self.log.emit(
                f"연결 실패: timeout ({BLE_CONNECT_TIMEOUT_SEC:.0f}s 초과) "
                "— ESP32가 service discovery 중 끊기면 이 오류가 발생합니다."
            )
            self.status_changed.emit("연결 실패")
            self.connected_changed.emit(False)
        except Exception:  # noqa: BLE001 — step별 상세 로그는 _connect_and_discover에서 출력
            self._client = None
            self._address = None
            self.status_changed.emit("연결 실패")
            self.connected_changed.emit(False)
        finally:
            if not connected and self._client is not None:
                try:
                    async def _cleanup() -> None:
                        if self._client is not None and self._client.is_connected:
                            await self._client.disconnect()

                    self._run_async(_cleanup())
                except Exception:  # noqa: BLE001
                    pass
                self._client = None
                self._address = None
            self.connect_finished.emit(connected)

    def _on_ble_disconnected(self, _client) -> None:
        """BleakClient disconnect callback — 예기치 않은 연결 끊김 처리."""
        self.log.emit("[BLE] 연결 끊김")
        self._client = None
        self._address = None
        self._clear_gatt_cache()
        self.connected_changed.emit(False)
        if self._read_in_progress:
            message = "읽기 실패, 재연결 필요"
            self.read_failed.emit(message)
            self.status_changed.emit(message)
        else:
            self.status_changed.emit("연결 안 됨")

    @Slot()
    def disconnect_device(self) -> None:
        if self._client is None:
            self.connected_changed.emit(False)
            self.status_changed.emit("연결 안 됨")
            return
        if self._loop is None:
            return

        async def _disconnect() -> None:
            if self._client is not None and self._client.is_connected:
                await self._client.disconnect()

        try:
            self._run_async(_disconnect())
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"해제 오류: {exc}")
        finally:
            self._client = None
            self._address = None
            self._clear_gatt_cache()
            self.log.emit("연결 해제")
            self.status_changed.emit("연결 안 됨")
            self.connected_changed.emit(False)

    @Slot()
    def read_config(self) -> None:
        if self._loop is None:
            self.log.emit("[BLE] 읽기 실패: asyncio loop 없음")
            self.read_failed.emit("읽기 실패, 재연결 필요")
            self.status_changed.emit("읽기 실패, 재연결 필요")
            return

        async def _read_with_retry() -> dict | None:
            self._read_in_progress = True
            try:
                if self._client is None or not self._client.is_connected:
                    if BLE_READ_AUTO_RECONNECT and self._last_connect_address:
                        await self._reconnect_last_device()
                    else:
                        raise ConnectionError("BLE client 없음 또는 연결 끊김")

                try:
                    return await self._perform_read_once()
                except Exception as exc:
                    if (
                        BLE_READ_AUTO_RECONNECT
                        and self._is_ble_cancelled_error(exc)
                        and self._last_connect_address
                    ):
                        self.log.emit(
                            f"[BLE] read WinError 취소 감지, 재연결 후 재시도: {exc}"
                        )
                        await self._reconnect_last_device()
                        await asyncio.sleep(BLE_READ_DELAY_SEC)
                        return await self._perform_read_once()
                    raise
            finally:
                self._read_in_progress = False

        if self._client is None and not self._last_connect_address:
            self.log.emit("[BLE] 읽기 실패: BLE client 없음")
            self.read_failed.emit("읽기 실패, 재연결 필요")
            self.status_changed.emit("읽기 실패, 재연결 필요")
            return

        try:
            config = self._run_async(_read_with_retry())
        except ConnectionError as exc:
            self.log.emit(f"[BLE] 읽기 실패: BLE 연결 끊김 - {exc}")
            self.read_failed.emit("읽기 실패, 재연결 필요")
            self.status_changed.emit("읽기 실패, 재연결 필요")
            return
        except Exception as exc:  # noqa: BLE001
            detail = (
                f"Windows BLE 작업 취소 ({exc})"
                if self._is_ble_cancelled_error(exc)
                else str(exc)
            )
            self.log.emit(f"[BLE] 설정 읽기 실패: {detail}")
            self.read_failed.emit("읽기 실패, 재연결 필요")
            self.status_changed.emit("읽기 실패, 재연결 필요")
            return

        if config is None:
            self.log.emit("[BLE] 설정 읽기 성공: READ_OK test only")
            self.status_changed.emit("읽기 성공 (READ_OK)")
            return

        messages = config.get("messages", [])
        phones = config.get("phones", [])
        self.log.emit("[BLE] 설정 읽기 성공: config loaded")
        self.log.emit(f"[BLE] messages count={len(messages)}")
        self.log.emit(f"[BLE] phones count={len(phones)}")
        self.status_changed.emit("설정 읽기 성공")
        self.config_loaded.emit(config)

    @Slot(str)
    def write_config(self, json_str: str) -> None:
        if self._client is None:
            self.log.emit("[BLE] 저장 실패: BLE client 없음")
            self.save_finished.emit(False, "BLE client 없음")
            return
        if not self._client.is_connected:
            self.log.emit("[BLE] 저장 실패: BLE 연결 끊김 (is_connected=False)")
            self.save_finished.emit(False, "BLE 연결 끊김")
            return
        if self._loop is None:
            self.log.emit("[BLE] 저장 실패: asyncio loop 없음")
            self.save_finished.emit(False, "loop 없음")
            return

        try:
            config = json.loads(json_str)
        except json.JSONDecodeError as exc:
            self.log.emit(f"[BLE] 저장 실패: JSON 파싱 오류 - {exc}")
            self.save_finished.emit(False, "JSON 파싱 오류")
            return

        full_payload = json.dumps(config, ensure_ascii=False).encode("utf-8")
        short_payload = json.dumps(build_short_test_config(), ensure_ascii=False).encode("utf-8")

        self.log.emit("설정 저장 요청")
        self.log.emit(f"[BLE] expected write uuid: {BLE_CHAR_WRITE_UUID}")
        self.log.emit(f"[BLE] write mode: response={BLE_WRITE_USE_RESPONSE}")

        async def _write() -> tuple[bool, str]:
            assert self._client is not None
            client = self._client
            status_char = self._ensure_status_characteristic(client)
            status_queue: asyncio.Queue[str] = asyncio.Queue()

            def _on_status_notify(_sender, data: bytearray) -> None:
                text = data.decode("utf-8", errors="replace").strip()
                self.log.emit(f"[BLE] status notify: {text}")
                status_queue.put_nowait(text)

            self._current_save_step = "status notify subscribe"
            if not client.is_connected:
                raise ConnectionError("BLE client disconnected before status subscribe")
            await client.start_notify(status_char, _on_status_notify)
            self.log.emit(f"[BLE] status notify subscribed: {status_char.uuid}")

            try:
                self._current_save_step = "short config write"
                short_status = await self._write_and_wait_status_notify(
                    client, short_payload, "short config test", status_queue
                )
                if short_status != "OK":
                    return False, f"short config failed: {short_status}"

                self._current_save_step = "full config write"
                final_status = await self._write_and_wait_status_notify(
                    client, full_payload, "full config", status_queue
                )
                return final_status == "OK", final_status
            finally:
                try:
                    if client.is_connected:
                        await client.stop_notify(status_char)
                        self.log.emit("[BLE] status notify unsubscribed")
                except Exception as exc:  # noqa: BLE001
                    self.log.emit(f"[BLE] status notify unsubscribe: {exc}")

        try:
            ok, status = self._run_async(_write())
        except Exception as exc:  # noqa: BLE001
            detail = self._format_save_error(exc, self._current_save_step or "unknown")
            self.log.emit(f"[BLE] 설정 저장 실패: {detail}")
            self.status_changed.emit("설정 저장 실패")
            self.save_finished.emit(False, detail)
            return

        if ok:
            self.log.emit("설정 저장 성공")
            self.status_changed.emit("설정 저장 성공")
        else:
            self.log.emit(f"설정 저장 실패: {status}")
            self.status_changed.emit("설정 저장 실패")
        self.save_finished.emit(ok, status)


class BleSettingsWidget(QWidget):
    """BLE configuration page widget."""

    def __init__(self, log_callback) -> None:
        super().__init__()
        self._log = log_callback
        self._ble_thread = QThread(self)
        self._ble_worker = BleAsyncWorker()
        self._ble_worker.moveToThread(self._ble_thread)
        self._ble_thread.started.connect(self._ble_worker.init_loop)
        self._ble_thread.start()

        self._message_inputs: list[QLineEdit] = []
        self._phone_inputs: list[QLineEdit] = []
        self._length_labels: list[QLabel] = []
        self._connecting = False

        self._build_ui()
        self._wire_signals()
        self.restore_defaults()
        self._on_connected_changed(False)

    def shutdown(self) -> None:
        QMetaObject.invokeMethod(
            self._ble_worker,
            "disconnect_device",
            Qt.BlockingQueuedConnection,
        )
        self._ble_thread.quit()
        self._ble_thread.wait(2000)

    def _ble_log(self, message: str) -> None:
        self._log("BLE", message)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(5)
        root.setContentsMargins(4, 4, 4, 4)

        conn_group = QGroupBox("BLE 연결")
        conn_layout = QGridLayout(conn_group)
        conn_layout.setHorizontalSpacing(6)
        conn_layout.setVerticalSpacing(5)
        self.scan_button = QPushButton("BLE 장치 검색")
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(180)
        self.device_combo.setMinimumHeight(34)
        self.connect_button = QPushButton("BLE 연결")
        self.disconnect_button = QPushButton("BLE 해제")
        for conn_button in (self.scan_button, self.connect_button, self.disconnect_button):
            conn_button.setMinimumHeight(34)
            conn_button.setMaximumHeight(39)
        self.ble_status_label = QLabel("연결 안 됨")
        self.ble_status_label.setObjectName("StatusBadge")

        conn_layout.addWidget(self.scan_button, 0, 0)
        conn_layout.addWidget(self.device_combo, 0, 1, 1, 2)
        conn_layout.addWidget(self.connect_button, 0, 3)
        conn_layout.addWidget(self.disconnect_button, 0, 4)
        conn_layout.addWidget(self.ble_status_label, 1, 0, 1, 5)
        root.addWidget(conn_group)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(4)
        scroll_layout.setContentsMargins(0, 0, 0, 0)

        msg_group = QGroupBox("알람 문구 설정")
        msg_layout = QGridLayout(msg_group)
        msg_layout.setHorizontalSpacing(6)
        msg_layout.setVerticalSpacing(4)
        for index, label_text in enumerate(MSG_LABELS):
            label = QLabel(label_text)
            label.setMinimumWidth(130)
            edit = QLineEdit()
            edit.setToolTip(f"기본: {DEFAULT_MESSAGES[index]}")
            edit.setMinimumHeight(30)
            edit.setMaximumHeight(34)
            edit.textChanged.connect(lambda _text, idx=index: self._update_length_warning(idx))
            length_label = QLabel("")
            length_label.setObjectName("GpsHint")
            self._message_inputs.append(edit)
            self._length_labels.append(length_label)
            msg_layout.addWidget(label, index, 0)
            msg_layout.addWidget(edit, index, 1)
            msg_layout.addWidget(length_label, index, 2)
        msg_layout.setColumnStretch(1, 1)
        scroll_layout.addWidget(msg_group)

        phone_group = QGroupBox("전화번호 설정 (최대 10개)")
        phone_layout = QGridLayout(phone_group)
        phone_layout.setHorizontalSpacing(6)
        phone_layout.setVerticalSpacing(4)
        for index in range(PHONE_COUNT):
            label = QLabel(f"전화번호 {index + 1}")
            edit = QLineEdit()
            edit.setPlaceholderText("예: 01012345678")
            edit.setMinimumHeight(30)
            edit.setMaximumHeight(34)
            self._phone_inputs.append(edit)
            if index < 5:
                row = index
                col_offset = 0
            else:
                row = index - 5
                col_offset = 3
            phone_layout.addWidget(label, row, col_offset)
            phone_layout.addWidget(edit, row, col_offset + 1)
        scroll_layout.addWidget(phone_group)
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        root.addWidget(scroll, stretch=1)

        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        self.read_button = QPushButton("BLE에서 설정 읽기")
        self.save_button = QPushButton("BLE로 설정 저장")
        self.defaults_button = QPushButton("기본값 복원")
        self.view_button = QPushButton("현재 설정 보기")
        for button in (self.read_button, self.save_button, self.defaults_button, self.view_button):
            button.setMinimumHeight(34)
            button.setMaximumHeight(39)
            button_row.addWidget(button)
        root.addLayout(button_row)

    def _wire_signals(self) -> None:
        self.scan_button.clicked.connect(
            self._ble_worker.scan_devices,
            Qt.ConnectionType.QueuedConnection,
        )
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.disconnect_button.clicked.connect(
            self._ble_worker.disconnect_device,
            Qt.ConnectionType.QueuedConnection,
        )
        self.read_button.clicked.connect(
            self._ble_worker.read_config,
            Qt.ConnectionType.QueuedConnection,
        )
        self.save_button.clicked.connect(self._on_save_clicked)
        self.defaults_button.clicked.connect(self.restore_defaults)
        self.view_button.clicked.connect(self.show_current_settings)

        self._ble_worker.log.connect(self._ble_log)
        self._ble_worker.status_changed.connect(self.ble_status_label.setText)
        self._ble_worker.devices_found.connect(self._on_devices_found)
        self._ble_worker.config_loaded.connect(self._apply_config)
        self._ble_worker.connected_changed.connect(self._on_connected_changed)
        self._ble_worker.connect_finished.connect(self._on_connect_finished)
        self._ble_worker.save_finished.connect(self._on_save_finished)
        self._ble_worker.read_failed.connect(self._on_read_failed)

    def _on_connect_clicked(self) -> None:
        if self._connecting:
            return

        address = self.device_combo.currentData()
        if not address:
            QMessageBox.warning(self, "BLE", "연결할 장치를 선택하세요.")
            return

        label = self.device_combo.currentText()
        display_name = BLE_DEVICE_NAME if BLE_DEVICE_NAME in label else label.split(" (")[0]

        self._connecting = True
        self._ble_log(f"연결 시도: {display_name}")
        self._ble_log(f"address={address}")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.ble_status_label.setText("연결 중...")

        QMetaObject.invokeMethod(
            self._ble_worker,
            "connect_device",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, address),
            Q_ARG(str, display_name),
        )

    def _on_connect_finished(self, success: bool) -> None:
        self._connecting = False
        if not success:
            self.connect_button.setEnabled(True)
            self.scan_button.setEnabled(True)
            self.disconnect_button.setEnabled(False)

    def _on_devices_found(self, devices: list) -> None:
        self.device_combo.clear()
        target_index = -1
        for index, device in enumerate(devices):
            self.device_combo.addItem(device.display_label, device.address)
            if device.is_target and target_index < 0:
                target_index = index
        if target_index >= 0:
            self.device_combo.setCurrentIndex(target_index)

    def _on_connected_changed(self, connected: bool) -> None:
        self.connect_button.setEnabled(not connected and not self._connecting)
        self.disconnect_button.setEnabled(connected)
        self.scan_button.setEnabled(not connected and not self._connecting)
        self.read_button.setEnabled(connected)
        self.save_button.setEnabled(connected)

    def _on_save_finished(self, ok: bool, status: str) -> None:
        if not ok:
            QMessageBox.warning(self, "BLE 저장", f"저장 결과: {status}")

    def _on_read_failed(self, message: str) -> None:
        self.ble_status_label.setText(message)

    def _update_length_warning(self, index: int) -> None:
        text = self._message_inputs[index].text()
        byte_len = message_byte_length(text)
        label = self._length_labels[index]
        if byte_len > MSG_MAX_BYTES:
            label.setText(f"경고: UTF-8 {byte_len}바이트 (최대 {MSG_MAX_BYTES}바이트)")
            label.setStyleSheet("color: #ff8080;")
        elif byte_len > 0:
            label.setText(f"{byte_len}/{MSG_MAX_BYTES}B")
            label.setStyleSheet("")
        else:
            label.setText("")
            label.setStyleSheet("")

    def _collect_messages(self) -> list[str]:
        return [edit.text().strip() for edit in self._message_inputs]

    def _collect_phones(self) -> list[str]:
        return [normalize_phone(edit.text()) for edit in self._phone_inputs]

    def _validate_before_save(self) -> bool:
        for index, message in enumerate(self._collect_messages()):
            if message_byte_length(message) > MSG_MAX_BYTES:
                QMessageBox.warning(
                    self,
                    "입력 오류",
                    f"알람 문구 {index + 1}이(가) 최대 길이({MSG_MAX_BYTES}바이트)를 초과했습니다.",
                )
                return False
        return True

    def _on_save_clicked(self) -> None:
        if not self._validate_before_save():
            return
        config = build_config_dict(self._collect_messages(), self._collect_phones())
        json_str = json.dumps(config, ensure_ascii=False)
        QMetaObject.invokeMethod(
            self._ble_worker,
            "write_config",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, json_str),
        )

    def _apply_config(self, config: dict) -> None:
        messages = config.get("messages", [])
        phones = config.get("phones", [])
        for index, edit in enumerate(self._message_inputs):
            if index < len(messages):
                edit.setText(str(messages[index]))
        for index, edit in enumerate(self._phone_inputs):
            if index < len(phones):
                edit.setText(str(phones[index]))
        self._ble_log("[BLE] UI updated")

    def restore_defaults(self) -> None:
        for index, edit in enumerate(self._message_inputs):
            edit.setText(DEFAULT_MESSAGES[index])
        for index, edit in enumerate(self._phone_inputs):
            edit.setText(DEFAULT_PHONES[index])

    def show_current_settings(self) -> None:
        config = build_config_dict(self._collect_messages(), self._collect_phones())
        pretty = json.dumps(config, ensure_ascii=False, indent=2)
        self._ble_log("현재 설정:")
        for line in pretty.splitlines():
            self._ble_log(line)
