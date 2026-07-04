"""
Windows Cat.M1 LTE modem SMS and GPS test program.

This program is intentionally small and beginner-friendly:
- Select a COM port and baudrate.
- Open/close the serial connection.
- Send basic AT commands.
- Send SMS messages using AT commands.
- Start/stop Woori-Net GPS and show the current position.
- Show every transmitted and received line in the log window.

The target device is a Woori-Net LTE Cat.M1 terminal that appears as a
Windows COM port in Device Manager.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import serial
from serial.tools import list_ports

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_TITLE = "Guro Mulsan Cat.M1 SMS Tester"
DEFAULT_BAUDRATE = "115200"
READ_TIMEOUT_SEC = 0.15
SMS_RESPONSE_TIMEOUT_SEC = 45
GPS_RESPONSE_TIMEOUT_SEC = 10
GPS_COORD_DECIMALS = 6
GPS_HDOP_WARN_THRESHOLD = 2.0


@dataclass(frozen=True)
class GpsFix:
    """Parsed Woori-Net MODE 1 ($$GPS) line."""

    fixed: bool
    latitude: float | None
    longitude: float | None
    altitude_m: float | None
    speed_kmh: int | None
    heading: int | None
    hdop: float | None
    date_str: str
    time_str: str
    raw_line: str
    satellite_count: int | None = None


def parse_woorinet_gps_line(line: str, coord_decimals: int = GPS_COORD_DECIMALS) -> GpsFix | None:
    """Parse one $$GPS line from Woori-Net MODE 1 output."""
    if not line.startswith("$$GPS"):
        return None

    parts = line.split(",")
    if len(parts) < 10:
        return None

    fix_status = None
    fix_index = None
    for index in range(3, len(parts)):
        field = parts[index].strip()
        if field == "A":
            fix_status = "A"
            fix_index = index
            break
        if field == "V":
            fix_status = "V"
            fix_index = index
            break
    if fix_status is None:
        return None

    date_str = parts[1].strip()
    time_str = parts[2].strip()

    def decode_coord(raw: str) -> float | None:
        if not raw:
            return None
        try:
            return int(raw) / (10**coord_decimals)
        except ValueError:
            return None

    def decode_altitude(raw: str) -> float | None:
        if not raw:
            return None
        try:
            return int(raw) / 10.0
        except ValueError:
            return None

    def decode_hdop(raw: str) -> float | None:
        if not raw:
            return None
        try:
            return int(raw) / 10.0
        except ValueError:
            return None

    def decode_int(raw: str) -> int | None:
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    satellite_count = None
    for index, field in enumerate(parts):
        if "-" not in field:
            continue
        sat_id, _, cno = field.partition("-")
        if not (sat_id.isdigit() and cno.isdigit()):
            continue
        if index > 0:
            previous = parts[index - 1].strip()
            if previous.isdigit():
                count = int(previous)
                if 0 <= count <= 40:
                    satellite_count = count
        break

    latitude = decode_coord(parts[3].strip())
    longitude = decode_coord(parts[4].strip())

    # Some firmware builds may report coordinates before the fix flag flips to A.
    has_coordinates = latitude is not None and longitude is not None
    is_fixed = fix_status == "A" or (fix_status == "V" and has_coordinates)

    return GpsFix(
        fixed=is_fixed,
        latitude=latitude if is_fixed else None,
        longitude=longitude if is_fixed else None,
        altitude_m=decode_altitude(parts[5].strip()) if is_fixed else None,
        speed_kmh=decode_int(parts[6].strip()) if is_fixed else None,
        heading=decode_int(parts[7].strip()) if is_fixed else None,
        hdop=decode_hdop(parts[8].strip()) if is_fixed else None,
        date_str=date_str,
        time_str=time_str,
        raw_line=line,
        satellite_count=satellite_count,
    )


def format_gps_log_message(fix: GpsFix) -> str:
    """Format one GPS update for the communication log."""
    status_flag = "A" if fix.fixed else "V"
    satellites = f"{fix.satellite_count}개" if fix.satellite_count is not None else "-"

    if fix.fixed and fix.latitude is not None and fix.longitude is not None:
        altitude = f"{fix.altitude_m:.1f}m" if fix.altitude_m is not None else "-"
        hdop = f"{fix.hdop:.1f}" if fix.hdop is not None else "-"
        return (
            f"[{status_flag}] Fixed | 위도={fix.latitude:.6f} | 경도={fix.longitude:.6f} "
            f"| 고도={altitude} | HDOP={hdop} | 위성={satellites}"
        )

    return f"[{status_flag}] Not Fixed | 위도=- | 경도=- | 위성={satellites} | {fix.raw_line}"


class SerialWorker(QObject):
    """Owns the serial port and performs blocking reads/writes off the UI thread."""

    log = Signal(str, str)
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    command_finished = Signal()
    gps_update = Signal(object)
    gps_state_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.serial_port: serial.Serial | None = None
        self.keep_reading = False
        self.read_timer: QTimer | None = None
        self.gps_running = False
        self.coord_decimals = GPS_COORD_DECIMALS

    @Slot(str, int)
    def connect_port(self, port_name: str, baudrate: int) -> None:
        """Open the selected COM port."""
        if self.serial_port and self.serial_port.is_open:
            self.error.emit("이미 연결되어 있습니다.")
            return

        try:
            self.serial_port = serial.Serial(
                port=port_name,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=READ_TIMEOUT_SEC,
                write_timeout=3,
            )
            self.keep_reading = True
            self.connected.emit(f"{port_name} @ {baudrate}")
            self.log.emit("SYSTEM", "시리얼 포트를 열었습니다.")
        except serial.SerialException as exc:
            self.error.emit(f"포트를 열 수 없습니다: {exc}")

    @Slot()
    def disconnect_port(self) -> None:
        """Close the serial port."""
        self.keep_reading = False
        if self.gps_running:
            self.gps_running = False
            self.gps_state_changed.emit(False)
        if self.serial_port:
            try:
                if self.serial_port.is_open:
                    self.serial_port.close()
                    self.log.emit("SYSTEM", "시리얼 포트를 닫았습니다.")
            except serial.SerialException as exc:
                self.error.emit(f"포트를 닫는 중 오류가 발생했습니다: {exc}")
        self.serial_port = None
        self.disconnected.emit()

    @Slot()
    def stop_worker(self) -> None:
        """Stop background polling before the application exits."""
        if self.read_timer is not None:
            self.read_timer.stop()
        self.disconnect_port()

    @Slot()
    def start_reader(self) -> None:
        """Start non-blocking serial polling inside the worker thread."""
        self.read_timer = QTimer(self)
        self.read_timer.setInterval(50)
        self.read_timer.timeout.connect(self.poll_serial)
        self.read_timer.start()

    @Slot()
    def poll_serial(self) -> None:
        """Poll the modem for any incoming lines."""
        if not self.keep_reading or not self.serial_port or not self.serial_port.is_open:
            return

        try:
            while self.serial_port.in_waiting > 0:
                data = self.serial_port.readline()
                if not data:
                    break
                text = self._decode(data)
                if text:
                    self._handle_rx_line(text)
        except serial.SerialException as exc:
            self.error.emit(f"수신 오류: {exc}")
            self.disconnect_port()

    @Slot(str)
    def send_at_command(self, command: str) -> None:
        """Send one AT command terminated with carriage return."""
        if not self._ensure_open():
            return

        cleaned = command.strip()
        if not cleaned:
            self.error.emit("전송할 AT 명령이 비어 있습니다.")
            return

        try:
            line = f"{cleaned}\r".encode("ascii", errors="ignore")
            self.serial_port.write(line)
            self.serial_port.flush()
            self.log.emit("TX", cleaned)
        except serial.SerialException as exc:
            self.error.emit(f"AT 명령 전송 실패: {exc}")
        finally:
            self.command_finished.emit()

    @Slot(int)
    def start_gps(self, interface: int) -> None:
        """Configure and start Woori-Net GPS (MODE 1) on the selected output interface."""
        if not self._ensure_open():
            self.command_finished.emit()
            return
        if self.gps_running:
            self.error.emit("GPS가 이미 실행 중입니다.")
            self.command_finished.emit()
            return

        try:
            self.keep_reading = False
            self._drain_serial_buffer()

            version = self._write_command_and_wait("AT$$GPSVER?", ["$$GPSVER", "ERROR"], timeout_sec=5)
            if "$$GPSVER" in version:
                self.log.emit("SYSTEM", f"GPS 버전: {version.split(':')[-1].strip()}")

            conf = f"AT$$GPSCONF={interface},0,1000,252,0,{self.coord_decimals},1,1"
            result = self._write_command_and_wait(conf, ["$$GPSCONF", "OK", "ERROR"], timeout_sec=GPS_RESPONSE_TIMEOUT_SEC)
            if "ERROR" in result and "OK" not in result:
                self.error.emit(f"GPS 설정 실패 (interface={interface}). 다른 출력 인터페이스를 선택해 보세요.")
                return

            result = self._write_command_and_wait("AT$$GPSMODE=1", ["$$GPSMODE", "OK", "ERROR"], timeout_sec=GPS_RESPONSE_TIMEOUT_SEC)
            if "ERROR" in result and "OK" not in result:
                self.error.emit("GPS MODE 설정에 실패했습니다.")
                return

            result = self._write_command_and_wait("AT$$GPS", ["OK", "ERROR"], timeout_sec=GPS_RESPONSE_TIMEOUT_SEC)
            if "ERROR" in result and "OK" not in result:
                self.error.emit("GPS 측위 시작 명령이 실패했습니다.")
                return

            self.gps_running = True
            self.gps_state_changed.emit(True)
            self.log.emit("SYSTEM", "GPS 측위를 시작했습니다. 실외 또는 창가에서 수신 대기 중...")
        except serial.SerialException as exc:
            self.error.emit(f"GPS 시작 중 시리얼 오류: {exc}")
        finally:
            if self.serial_port and self.serial_port.is_open:
                self.keep_reading = True
            self.command_finished.emit()

    @Slot()
    def stop_gps(self) -> None:
        """Stop Woori-Net GPS positioning."""
        if not self._ensure_open():
            self.command_finished.emit()
            return

        try:
            self.keep_reading = False
            self._write_command_and_wait("AT$$GPSSTOP", ["OK", "ERROR"], timeout_sec=GPS_RESPONSE_TIMEOUT_SEC)
            self.gps_running = False
            self.gps_state_changed.emit(False)
            self.log.emit("SYSTEM", "GPS 측위를 중단했습니다.")
        except serial.SerialException as exc:
            self.error.emit(f"GPS 중단 중 시리얼 오류: {exc}")
        finally:
            if self.serial_port and self.serial_port.is_open:
                self.keep_reading = True
            self.command_finished.emit()

    @Slot(str, str)
    def send_sms(self, phone_number: str, message: str) -> None:
        """
        Send SMS in AT text mode.

        Sequence:
        1. ATE0       : echo off, makes logs easier to read.
        2. AT+CMGF=1 : SMS text mode.
        3. AT+CSCS="GSM" : common character set for simple English/numeric SMS.
        4. AT+CMGS="number"
        5. message + Ctrl+Z

        Korean text often needs modem-specific UCS2 settings. This beginner tool
        sends text mode first because it is easiest to test and understand.
        """
        if not self._ensure_open():
            return

        phone_number = self._normalize_phone_number(phone_number)
        message = message.strip()
        if not phone_number:
            self.error.emit("전화번호를 입력하세요.")
            return
        if not message:
            self.error.emit("문자 내용을 입력하세요.")
            return

        try:
            # SMS sending is a prompt-based sequence. Pause the normal reader
            # so each response is consumed by _wait_for in the correct order.
            self.keep_reading = False
            self._write_command("ATE0")
            self._wait_for(["OK"], timeout_sec=5)

            # Enable verbose modem errors so the log shows the exact SMS failure cause.
            self._write_command("AT+CMEE=2")
            self._wait_for(["OK"], timeout_sec=5)

            self._write_command("AT+CMGF=1")
            self._wait_for(["OK"], timeout_sec=5)

            # Use UCS2 automatically when the text contains non-ASCII characters.
            # This is important for Korean SMS tests.
            if self._needs_ucs2(message):
                payload_text = self._encode_ucs2(message)
                cmgs_number = self._encode_ucs2(phone_number)
                toda = 145 if phone_number.startswith("+") else 129
                self._write_command('AT+CSCS="UCS2"')
                self._wait_for(["OK"], timeout_sec=5)
                self._write_command("AT+CSMP=17,167,0,8")
                self._wait_for(["OK"], timeout_sec=5)
                self.log.emit("SYSTEM", "한글/특수문자 감지: UCS2 모드로 전송합니다.")
            else:
                payload_text = message
                cmgs_number = phone_number
                toda = 145 if phone_number.startswith("+") else 129
                self._write_command('AT+CSCS="GSM"')
                self._wait_for(["OK"], timeout_sec=5)
                self._write_command("AT+CSMP=17,167,0,0")
                self._wait_for(["OK"], timeout_sec=5)

            # Check the SMS service center address. Some modem/SIM combinations fail
            # to send until a valid SMSC value is provisioned.
            self._write_command("AT+CSCA?")
            self._wait_for(["OK", "ERROR", "+CMS ERROR", "+CME ERROR"], timeout_sec=5)

            self.log.emit("SYSTEM", f"SMS 대상 번호 형식: TODA={toda}")
            self._write_command(f'AT+CMGS="{cmgs_number}",{toda}')
            prompt = self._wait_for([">"], timeout_sec=10)
            if ">" not in prompt:
                self.error.emit("모뎀이 SMS 입력 프롬프트(>)를 보내지 않았습니다.")
                return

            payload = f"{payload_text}\x1A".encode("ascii", errors="replace")
            self.serial_port.write(payload)
            self.serial_port.flush()
            self.log.emit("TX", f"{payload_text} <Ctrl+Z>")

            result = self._wait_for(["OK", "ERROR", "+CMS ERROR", "+CME ERROR"], timeout_sec=SMS_RESPONSE_TIMEOUT_SEC)
            if "OK" in result:
                self.log.emit("SYSTEM", "SMS 전송 명령이 완료되었습니다.")
            else:
                self.error.emit("SMS 전송 실패 또는 모뎀 오류가 반환되었습니다.")
        except serial.SerialException as exc:
            self.error.emit(f"SMS 전송 중 시리얼 오류: {exc}")
        finally:
            if self.serial_port and self.serial_port.is_open:
                self.keep_reading = True
            self.command_finished.emit()

    def _ensure_open(self) -> bool:
        """Return True only when a usable serial port is open."""
        if self.serial_port and self.serial_port.is_open:
            return True
        self.error.emit("먼저 COM 포트에 연결하세요.")
        return False

    def _write_command(self, command: str) -> None:
        """Write an AT command and log it."""
        assert self.serial_port is not None
        self.serial_port.write(f"{command}\r".encode("ascii", errors="ignore"))
        self.serial_port.flush()
        self.log.emit("TX", command)

    def _wait_for(self, expected_tokens: list[str], timeout_sec: float) -> str:
        """
        Read modem output until one of the expected tokens appears or time runs out.

        The reader thread is temporarily stopped during SMS sending, so this method
        can safely collect command responses in sequence.
        """
        assert self.serial_port is not None
        deadline = time.monotonic() + timeout_sec
        collected: list[str] = []

        while time.monotonic() < deadline:
            raw = self.serial_port.readline()
            if not raw:
                continue

            text = self._decode(raw)
            if text:
                collected.append(text)
                self._handle_rx_line(text)
                joined = "\n".join(collected)
                if any(token in joined for token in expected_tokens):
                    return joined

        joined = "\n".join(collected)
        self.log.emit("SYSTEM", f"응답 대기 시간 초과: {', '.join(expected_tokens)}")
        return joined

    def _handle_rx_line(self, text: str) -> None:
        """Log one RX line and emit parsed GPS updates when applicable."""
        fix = parse_woorinet_gps_line(text, self.coord_decimals)
        if fix is not None:
            self.gps_update.emit(fix)
            return

        self.log.emit("RX", text)

    def _drain_serial_buffer(self) -> None:
        """Discard unread bytes before a command sequence."""
        assert self.serial_port is not None
        while self.serial_port.in_waiting > 0:
            self.serial_port.readline()

    def _write_command_and_wait(self, command: str, expected_tokens: list[str], timeout_sec: float) -> str:
        """Send one AT command and collect the modem response."""
        self._write_command(command)
        return self._wait_for(expected_tokens, timeout_sec=timeout_sec)

    @staticmethod
    def _decode(data: bytes) -> str:
        """Decode modem bytes without crashing on unexpected characters."""
        for encoding in ("utf-8", "cp949", "ascii"):
            try:
                return data.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _needs_ucs2(text: str) -> bool:
        """Return True when the SMS body contains non-ASCII characters."""
        return any(ord(char) > 127 for char in text)

    @staticmethod
    def _encode_ucs2(text: str) -> str:
        """Encode text to uppercase UCS2 hex for AT+CSCS=\"UCS2\" text mode."""
        return text.encode("utf-16-be").hex().upper()

    @staticmethod
    def _normalize_phone_number(phone_number: str) -> str:
        """Keep only digits and a leading plus sign."""
        phone_number = phone_number.strip()
        if phone_number.startswith("+"):
            return "+" + "".join(char for char in phone_number[1:] if char.isdigit())
        return "".join(char for char in phone_number if char.isdigit())


class MainWindow(QMainWindow):
    """Main GUI window."""

    connect_requested = Signal(str, int)
    disconnect_requested = Signal()
    send_command_requested = Signal(str)
    send_sms_requested = Signal(str, str)
    start_gps_requested = Signal(int)
    stop_gps_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1280, 900)
        self.resize(1280, 900)

        self.last_gps_fix: GpsFix | None = None

        self.worker_thread = QThread(self)
        self.worker = SerialWorker()
        self.worker.moveToThread(self.worker_thread)

        self.connect_requested.connect(self.worker.connect_port)
        self.disconnect_requested.connect(self.worker.disconnect_port)
        self.send_command_requested.connect(self.worker.send_at_command)
        self.send_sms_requested.connect(self.worker.send_sms)
        self.start_gps_requested.connect(self.worker.start_gps)
        self.stop_gps_requested.connect(self.worker.stop_gps)
        self.worker.connected.connect(self.on_connected)
        self.worker.disconnected.connect(self.on_disconnected)
        self.worker.error.connect(self.on_error)
        self.worker.log.connect(self.append_log)
        self.worker.gps_update.connect(self.on_gps_update)
        self.worker.gps_state_changed.connect(self.on_gps_state_changed)
        self.worker.command_finished.connect(self.on_command_finished)
        self.worker_thread.started.connect(self.worker.start_reader)
        self.worker_thread.start()

        self._build_ui()
        self._apply_style()
        self.refresh_ports()
        self.on_disconnected()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
        """Close the COM port and worker thread when the app exits."""
        self.worker.stop_worker()
        self.worker_thread.quit()
        self.worker_thread.wait(1500)
        super().closeEvent(event)

    def _build_ui(self) -> None:
        """Create all widgets and layouts."""
        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(14, 12, 14, 14)
        main_layout.setSpacing(10)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)
        logo_label = QLabel()
        logo_path = Path(__file__).with_name("guro_logo.png")
        if logo_path.exists():
            pixmap = QPixmap(str(logo_path)).scaled(
                84,
                84,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            logo_label.setPixmap(pixmap)
            self.setWindowIcon(QIcon(str(logo_path)))
        logo_label.setFixedSize(84, 84)
        logo_label.setAlignment(Qt.AlignCenter)

        title_box = QVBoxLayout()
        title = QLabel("Cat.M1 LTE 모뎀 SMS 발송 테스트")
        title.setObjectName("Title")
        subtitle = QLabel("COM 포트 연결, AT 명령, SMS 전송, GPS 현재 위치 확인")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        header_layout.addWidget(logo_label)
        header_layout.addLayout(title_box)
        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        connection_group = QGroupBox("연결 설정")
        connection_layout = QGridLayout(connection_group)
        connection_layout.setHorizontalSpacing(8)
        connection_layout.setVerticalSpacing(8)

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(220)
        self.refresh_button = QPushButton("새로고침")
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", DEFAULT_BAUDRATE, "230400", "460800", "921600"])
        self.baud_combo.setCurrentText(DEFAULT_BAUDRATE)

        self.connect_button = QPushButton("연결")
        self.disconnect_button = QPushButton("해제")
        self.status_label = QLabel("연결 안 됨")
        self.status_label.setObjectName("StatusBadge")

        connection_layout.addWidget(QLabel("COM 포트"), 0, 0)
        connection_layout.addWidget(self.port_combo, 0, 1)
        connection_layout.addWidget(self.refresh_button, 0, 2)
        connection_layout.addWidget(QLabel("Baudrate"), 0, 3)
        connection_layout.addWidget(self.baud_combo, 0, 4)
        connection_layout.addWidget(self.connect_button, 0, 5)
        connection_layout.addWidget(self.disconnect_button, 0, 6)
        connection_layout.addWidget(self.status_label, 0, 7)
        connection_layout.setColumnStretch(8, 1)
        main_layout.addWidget(connection_group)

        content_layout = QHBoxLayout()
        content_layout.setSpacing(10)
        main_layout.addLayout(content_layout, stretch=1)

        left_layout = QVBoxLayout()
        left_layout.setSpacing(10)
        content_layout.addLayout(left_layout, stretch=1)

        command_group = QGroupBox("기본 AT 명령 테스트")
        command_layout = QGridLayout(command_group)
        command_layout.setHorizontalSpacing(8)
        command_layout.setVerticalSpacing(8)
        self.at_buttons: list[QPushButton] = []
        for index, command in enumerate(["AT", "ATI", "AT+CSQ", "AT+CREG?", "AT+COPS?"]):
            button = QPushButton(command)
            button.setMinimumHeight(38)
            button.clicked.connect(lambda checked=False, cmd=command: self.send_command(cmd))
            self.at_buttons.append(button)
            command_layout.addWidget(button, index // 3, index % 3)
        command_layout.setColumnStretch(0, 1)
        command_layout.setColumnStretch(1, 1)
        command_layout.setColumnStretch(2, 1)

        self.custom_command = QLineEdit()
        self.custom_command.setPlaceholderText("예: AT+CGATT?")
        self.custom_send_button = QPushButton("직접 전송")
        self.custom_send_button.setMinimumHeight(38)
        command_layout.addWidget(QLabel("직접 입력"), 2, 0)
        command_layout.addWidget(self.custom_command, 2, 1)
        command_layout.addWidget(self.custom_send_button, 2, 2)
        left_layout.addWidget(command_group)

        gps_group = QGroupBox("GPS 현재 위치")
        gps_layout = QGridLayout(gps_group)
        gps_layout.setHorizontalSpacing(10)
        gps_layout.setVerticalSpacing(10)

        self.gps_interface_combo = QComboBox()
        self.gps_interface_combo.addItem("USB Modem (AT) — 권장", 4)
        self.gps_interface_combo.addItem("UART2 (AT Command)", 1)

        self.gps_start_button = QPushButton("GPS 시작")
        self.gps_stop_button = QPushButton("GPS 중단")
        self.gps_maps_button = QPushButton("지도에서 보기")
        self.gps_status_banner = QLabel("GPS 미시작")
        self.gps_status_banner.setObjectName("GpsStatusIdle")
        self.gps_status_banner.setAlignment(Qt.AlignCenter)
        self.gps_status_banner.setMinimumHeight(64)
        self.gps_status_banner.setWordWrap(True)

        self.gps_hint_label = QLabel("연결 후 GPS 시작을 누르세요.")
        self.gps_hint_label.setObjectName("GpsHint")
        self.gps_hint_label.setWordWrap(True)

        self.gps_lat_value = QLabel("-")
        self.gps_lon_value = QLabel("-")
        self.gps_alt_value = QLabel("-")
        self.gps_hdop_value = QLabel("-")
        self.gps_sat_value = QLabel("-")
        self.gps_time_value = QLabel("-")
        self.gps_accuracy_value = QLabel("-")

        for label in (self.gps_lat_value, self.gps_lon_value):
            label.setObjectName("GpsCoord")
            label.setMinimumHeight(34)
            label.setAlignment(Qt.AlignCenter)
        for label in (
            self.gps_alt_value,
            self.gps_hdop_value,
            self.gps_sat_value,
            self.gps_time_value,
            self.gps_accuracy_value,
        ):
            label.setObjectName("GpsValue")
            label.setMinimumHeight(30)
            label.setAlignment(Qt.AlignCenter)

        gps_layout.addWidget(QLabel("출력 인터페이스"), 0, 0)
        gps_layout.addWidget(self.gps_interface_combo, 0, 1)
        gps_layout.addWidget(self.gps_start_button, 0, 2)
        gps_layout.addWidget(self.gps_stop_button, 0, 3)
        gps_layout.addWidget(self.gps_status_banner, 1, 0, 1, 4)
        gps_layout.addWidget(self.gps_hint_label, 2, 0, 1, 4)
        gps_layout.addWidget(QLabel("위도"), 3, 0)
        gps_layout.addWidget(self.gps_lat_value, 3, 1, 1, 3)
        gps_layout.addWidget(QLabel("경도"), 4, 0)
        gps_layout.addWidget(self.gps_lon_value, 4, 1, 1, 3)
        gps_layout.addWidget(QLabel("고도"), 5, 0)
        gps_layout.addWidget(self.gps_alt_value, 5, 1)
        gps_layout.addWidget(QLabel("HDOP"), 5, 2)
        gps_layout.addWidget(self.gps_hdop_value, 5, 3)
        gps_layout.addWidget(QLabel("위성"), 6, 0)
        gps_layout.addWidget(self.gps_sat_value, 6, 1)
        gps_layout.addWidget(QLabel("정확도"), 6, 2)
        gps_layout.addWidget(self.gps_accuracy_value, 6, 3)
        gps_layout.addWidget(QLabel("시각"), 7, 0)
        gps_layout.addWidget(self.gps_time_value, 7, 1, 1, 2)
        gps_layout.addWidget(self.gps_maps_button, 7, 3)

        center_layout = QVBoxLayout()
        center_layout.setSpacing(10)
        center_layout.addWidget(gps_group)
        center_layout.addStretch()

        sms_group = QGroupBox("문자 발송")
        sms_layout = QGridLayout(sms_group)
        self.phone_input = QLineEdit()
        self.phone_input.setPlaceholderText("예: 01012345678")
        self.message_input = QTextEdit()
        self.message_input.setPlaceholderText("문자 내용을 입력하세요. 기본 텍스트 모드는 영문/숫자 테스트를 권장합니다.")
        self.message_input.setFixedHeight(120)
        self.send_sms_button = QPushButton("SMS 보내기")
        self.send_sms_button.setObjectName("PrimaryButton")

        sms_layout.addWidget(QLabel("전화번호"), 0, 0)
        sms_layout.addWidget(self.phone_input, 0, 1)
        sms_layout.addWidget(QLabel("내용"), 1, 0)
        sms_layout.addWidget(self.message_input, 1, 1)
        sms_layout.addWidget(self.send_sms_button, 2, 1)
        left_layout.addWidget(sms_group)
        left_layout.addStretch()

        content_layout.addLayout(center_layout, stretch=1)

        log_group = QGroupBox("송신/수신 로그")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.clear_log_button = QPushButton("Log Clear")
        log_layout.addWidget(self.log_view)
        log_layout.addWidget(self.clear_log_button)
        content_layout.addWidget(log_group, stretch=2)

        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.connect_serial)
        self.disconnect_button.clicked.connect(lambda: self.disconnect_requested.emit())
        self.custom_send_button.clicked.connect(lambda: self.send_command(self.custom_command.text()))
        self.send_sms_button.clicked.connect(self.send_sms)
        self.gps_start_button.clicked.connect(self.start_gps)
        self.gps_stop_button.clicked.connect(self.stop_gps)
        self.gps_maps_button.clicked.connect(self.open_gps_map)
        self.clear_log_button.clicked.connect(self.log_view.clear)

    def _apply_style(self) -> None:
        """Apply a dark UI inspired by the reference desktop program."""
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #202020;
                color: #e8e8e8;
                font-family: "Segoe UI", "Malgun Gothic";
                font-size: 10pt;
            }
            QLabel#Title {
                font-size: 18pt;
                font-weight: 700;
                color: #ffffff;
            }
            QLabel#Subtitle {
                color: #a8a8a8;
            }
            QGroupBox {
                background: #2b2b2b;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                margin-top: 18px;
                padding: 12px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #f0f0f0;
            }
            QComboBox, QLineEdit, QTextEdit, QPlainTextEdit {
                background: #1b1b1b;
                border: 1px solid #3e3e3e;
                border-radius: 4px;
                color: #f4f4f4;
                padding: 7px;
                selection-background-color: #1689e8;
            }
            QPlainTextEdit {
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 10pt;
            }
            QPushButton {
                background: #343434;
                border: 1px solid #4a4a4a;
                border-radius: 4px;
                color: #eeeeee;
                padding: 8px 12px;
                min-height: 20px;
            }
            QPushButton:hover {
                background: #414141;
            }
            QPushButton:pressed {
                background: #1689e8;
            }
            QPushButton:disabled {
                background: #252525;
                color: #666666;
                border-color: #333333;
            }
            QPushButton#PrimaryButton {
                background: #1689e8;
                border-color: #1689e8;
                color: white;
                font-weight: 700;
            }
            QPushButton#PrimaryButton:hover {
                background: #239bf7;
            }
            QLabel#StatusBadge {
                background: #3a3a3a;
                border-radius: 10px;
                padding: 5px 10px;
                color: #dddddd;
                font-weight: 700;
            }
            QLabel#GpsStatusIdle {
                background: #3a3a3a;
                border: 2px solid #555555;
                border-radius: 8px;
                padding: 10px 12px;
                color: #dddddd;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel#GpsStatusRunning {
                background: #4a3a12;
                border: 2px solid #c9a227;
                border-radius: 8px;
                padding: 10px 12px;
                color: #ffe566;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel#GpsStatusFixed {
                background: #123d24;
                border: 2px solid #2fbf71;
                border-radius: 8px;
                padding: 10px 12px;
                color: #8dffb8;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel#GpsStatusWarn {
                background: #4a2412;
                border: 2px solid #e07b39;
                border-radius: 8px;
                padding: 10px 12px;
                color: #ffc58a;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel#GpsHint {
                color: #b8b8b8;
                font-size: 10pt;
                padding: 0 4px 4px 4px;
            }
            QLabel#GpsCoord {
                background: #141414;
                border: 2px solid #1689e8;
                border-radius: 6px;
                padding: 8px 10px;
                color: #ffffff;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel#GpsValue {
                background: #1b1b1b;
                border: 1px solid #3e3e3e;
                border-radius: 4px;
                padding: 6px 8px;
                color: #f0f0f0;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 11pt;
                font-weight: 600;
            }
            """
        )

    @Slot()
    def refresh_ports(self) -> None:
        """Refresh the COM port list from Windows."""
        current = self.port_combo.currentData()
        self.port_combo.clear()

        ports = list(list_ports.comports())
        for port in ports:
            label = f"{port.device} - {port.description}"
            self.port_combo.addItem(label, port.device)

        if not ports:
            self.port_combo.addItem("COM 포트를 찾을 수 없습니다.", "")
        else:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    @Slot()
    def connect_serial(self) -> None:
        """Request serial connection using the selected UI values."""
        port_name = self.port_combo.currentData()
        if not port_name:
            QMessageBox.warning(self, "COM 포트 없음", "Windows 장치관리자에서 모뎀 COM 포트를 확인하세요.")
            return

        baudrate = int(self.baud_combo.currentText())
        self.connect_button.setEnabled(False)
        self.connect_requested.emit(port_name, baudrate)

    @Slot(str)
    def send_command(self, command: str) -> None:
        """Send a basic or custom AT command."""
        self._set_busy(True)
        self.send_command_requested.emit(command)

    @Slot()
    def send_sms(self) -> None:
        """Send the SMS form values to the worker."""
        self._set_busy(True)
        self.send_sms_requested.emit(self.phone_input.text(), self.message_input.toPlainText())

    @Slot()
    def start_gps(self) -> None:
        """Start GPS positioning with the selected output interface."""
        interface = int(self.gps_interface_combo.currentData())
        self._set_busy(True)
        self.start_gps_requested.emit(interface)

    @Slot()
    def stop_gps(self) -> None:
        """Stop GPS positioning."""
        self._set_busy(True)
        self.stop_gps_requested.emit()

    @Slot()
    def open_gps_map(self) -> None:
        """Open the last fixed GPS coordinates in the default web browser."""
        if not self.last_gps_fix or not self.last_gps_fix.fixed:
            QMessageBox.information(self, "GPS", "먼저 GPS 측위가 완료되어야 합니다.")
            return
        if self.last_gps_fix.latitude is None or self.last_gps_fix.longitude is None:
            QMessageBox.information(self, "GPS", "표시할 좌표가 없습니다.")
            return

        url = f"https://www.google.com/maps?q={self.last_gps_fix.latitude:.6f},{self.last_gps_fix.longitude:.6f}"
        webbrowser.open(url)

    @Slot(str)
    def on_connected(self, status: str) -> None:
        """Update UI when connected."""
        self.status_label.setText(f"연결됨: {status}")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self._set_command_controls(True)
        self._set_gps_status_banner("Idle", "연결됨 — GPS 대기", hint="GPS 시작을 눌러 현재 위치 측위를 시작하세요.")

    @Slot()
    def on_disconnected(self) -> None:
        """Update UI when disconnected."""
        self.status_label.setText("연결 안 됨")
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self._set_command_controls(False)
        self._reset_gps_display()

    @Slot(bool)
    def on_gps_state_changed(self, running: bool) -> None:
        """Enable GPS controls according to the worker state."""
        connected = self.disconnect_button.isEnabled()
        if not connected:
            return
        self.gps_start_button.setEnabled(not running)
        self.gps_stop_button.setEnabled(running)
        self.gps_interface_combo.setEnabled(not running)
        if running:
            self._set_gps_status_banner(
                "Running",
                "● GPS 수신 중 — 측위 대기",
                hint="실외 또는 창가에서 위성 신호를 기다리는 중입니다.",
            )
        elif self.last_gps_fix is None:
            self._set_gps_status_banner(
                "Idle",
                "GPS 중지됨",
                hint="GPS 시작 버튼을 눌러 측위를 시작하세요.",
            )

    @Slot(object)
    def on_gps_update(self, fix: GpsFix) -> None:
        """Refresh the GPS panel from one parsed $$GPS line."""
        self.append_log("GPS", format_gps_log_message(fix))

        if fix.satellite_count is not None:
            self.gps_sat_value.setText(f"{fix.satellite_count}개")

        if fix.fixed and fix.latitude is not None and fix.longitude is not None:
            self.last_gps_fix = fix
            self._set_gps_status_banner(
                "Fixed",
                "● 측위 완료 (Fixed)",
                hint=f"위도 {fix.latitude:.6f}° / 경도 {fix.longitude:.6f}°",
            )
            self.gps_lat_value.setText(f"{fix.latitude:.6f}°")
            self.gps_lon_value.setText(f"{fix.longitude:.6f}°")
            self.gps_alt_value.setText(f"{fix.altitude_m:.1f} m" if fix.altitude_m is not None else "-")
            self.gps_hdop_value.setText(f"{fix.hdop:.1f}" if fix.hdop is not None else "-")
            if fix.date_str and fix.time_str and len(fix.date_str) == 8 and len(fix.time_str) == 6:
                self.gps_time_value.setText(
                    f"{fix.date_str[:4]}-{fix.date_str[4:6]}-{fix.date_str[6:8]} "
                    f"{fix.time_str[:2]}:{fix.time_str[2:4]}:{fix.time_str[4:6]} (KST)"
                )
            else:
                self.gps_time_value.setText("-")

            if fix.hdop is None:
                self.gps_accuracy_value.setText("정보 없음")
                self.gps_hint_label.setText("좌표가 표시되었습니다. 지도에서 보기로 위치를 확인할 수 있습니다.")
            elif fix.hdop <= GPS_HDOP_WARN_THRESHOLD:
                self.gps_accuracy_value.setText("양호")
                self.gps_hint_label.setText("신호 상태가 양호합니다.")
            else:
                self.gps_accuracy_value.setText(f"낮음 ({fix.hdop:.1f})")
                self.gps_hint_label.setText("HDOP가 높아 정확도가 낮을 수 있습니다. 개활지에서 다시 확인하세요.")

            self.gps_maps_button.setEnabled(True)
            return

        sat_text = f"{fix.satellite_count}개" if fix.satellite_count is not None else "확인 중"
        if fix.satellite_count == 0:
            self._set_gps_status_banner(
                "Warn",
                "● 측위 불가 — 위성 0개",
                hint="GPS 안테나·실외 환경을 확인하세요.",
            )
        elif fix.satellite_count is not None and fix.satellite_count < 4:
            self._set_gps_status_banner(
                "Running",
                f"● 측위 중 — 위성 {sat_text}",
                hint="위성은 보이지만 아직 좌표가 확정되지 않았습니다.",
            )
        else:
            self._set_gps_status_banner(
                "Running",
                f"● 측위 중 — 위성 {sat_text}",
                hint="위성은 보이지만 좌표는 Fixed(A) 전까지 비어 있습니다. 실외에서 1~5분 더 기다려 보세요.",
            )

        self.gps_accuracy_value.setText("대기 중")
        self.gps_maps_button.setEnabled(self.last_gps_fix is not None and self.last_gps_fix.fixed)

    @Slot(str)
    def on_error(self, message: str) -> None:
        """Show errors in both log and message box."""
        self.append_log("ERROR", message)
        QMessageBox.warning(self, "오류", message)
        self._set_busy(False)

    @Slot()
    def on_command_finished(self) -> None:
        """Re-enable controls after a command finishes."""
        self._set_busy(False)

    @Slot(str, str)
    def append_log(self, direction: str, message: str) -> None:
        """Append one timestamped log line."""
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] [{direction}] {message}")
        self.log_view.moveCursor(QTextCursor.End)

    def _set_command_controls(self, enabled: bool) -> None:
        """Enable or disable all controls that require an open COM port."""
        for button in self.at_buttons:
            button.setEnabled(enabled)
        self.custom_command.setEnabled(enabled)
        self.custom_send_button.setEnabled(enabled)
        self.phone_input.setEnabled(enabled)
        self.message_input.setEnabled(enabled)
        self.send_sms_button.setEnabled(enabled)
        self.gps_start_button.setEnabled(enabled)
        self.gps_stop_button.setEnabled(False)
        self.gps_interface_combo.setEnabled(enabled)
        self.gps_maps_button.setEnabled(enabled and self.last_gps_fix is not None and self.last_gps_fix.fixed)

    def _reset_gps_display(self) -> None:
        """Clear GPS values when the serial connection closes."""
        self.last_gps_fix = None
        self._set_gps_status_banner("Idle", "GPS 미시작", hint="연결 후 GPS 시작을 누르세요.")
        self.gps_lat_value.setText("-")
        self.gps_lon_value.setText("-")
        self.gps_alt_value.setText("-")
        self.gps_hdop_value.setText("-")
        self.gps_sat_value.setText("-")
        self.gps_time_value.setText("-")
        self.gps_accuracy_value.setText("-")
        self.gps_maps_button.setEnabled(False)

    def _set_gps_status_banner(self, state: str, title: str, hint: str = "") -> None:
        """Update the large GPS status banner and its color theme."""
        self.gps_status_banner.setText(title)
        self.gps_status_banner.setObjectName(f"GpsStatus{state}")
        self.gps_status_banner.style().unpolish(self.gps_status_banner)
        self.gps_status_banner.style().polish(self.gps_status_banner)
        if hint:
            self.gps_hint_label.setText(hint)

    def _set_busy(self, busy: bool) -> None:
        """Avoid duplicate sends while a command is in progress."""
        connected = self.disconnect_button.isEnabled()
        self._set_command_controls(connected and not busy)
        if connected:
            self.on_gps_state_changed(self.worker.gps_running)
            if busy:
                self.gps_start_button.setEnabled(False)
                self.gps_stop_button.setEnabled(False)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
