"""
Uniden Scanner Text Display - Qt version

Requires:
    python -m pip install PySide6 pyserial

Run:
    python uniden_text_display_qt.py

Build EXE:
    python -m pip install pyinstaller PySide6 pyserial
    python -m PyInstaller --onefile --windowed --name UnidenTextDisplayQt --add-data "uniden_text_display.ui;." uniden_text_display_qt.py
"""

from __future__ import annotations

import csv
import os
import queue
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtUiTools import QUiLoader

try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None

BAUD_CHOICES = ["115200", "57600", "38400", "19200", "9600"]
MODEL_CHOICES = [
    "SDS100/SDS200",
    "BCD996P2 / BCD325P2",
]

# 36 x 18 scan icon selected/tuned in the Tkinter build. Kept as a bitmap so it does not
# get redrawn with uneven canvas lines at small sizes.
SCAN_ICON_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAACQAAAASCAYAAAAzI3woAAACDElEQVR4nNWVvaoTQRTHz9mPBK4b7odEbFQEb2ujpQj3Ae4j2PgYFj6AlYKvYGcros3a2lgoghZCECEQbrI7s7M7O7M787fIBmMhZIMJ+oOBgXPY82POmR2ifwzukwzgoCzLj4PB4Lq1FgACZiYATEQBMzMAlyRJUJbleZIkr/oKRT3zmYiO4yW/Bay1ZK39lcjc99v9hSaTiRuPx2+aprlirUUnGDJz6Zy7E8fxVecclj4830bor5Dn+V1r7UVVVRYApJSPiYgAhDsvDiDoVkxEVBTFbWOM0Fp7AFBKPVvl7VxmTSokIprP59fquv6+JvOCiChN06gb9L3IBEREWZYdaa0/dbcNVVW9BhB2a28y3LVroLV+571H27bQWr+fTqeXuni0t3alaRoREVVV9RIAjDEwxnyRUo5Xwmvy+5FSSj1fydR1PRNCnHYnE1dVdQPAWyHEg05qq3/RxhRF8QQAtNaNtfbHbDY7XY9rrW8CgLXWZ1l2tk2NXsPXNA2MMW0YhmHbtt+Y+QMzHwBwAIiZR977+8PhMPTeX7RtezYajT5vI7YRWutVq/AnlFKQUrbd/mvfGr167JybO+eYiLhpGmJmD4CZlwfdzcwoiqLAGOOY+VFfoV4sFotDAIcAjgAc53l+AuBESjkWQlyWUt5TSnnvPYQQD3cqswla61vee0gpnxIRrZ6X/5qfKw69dCsm1GcAAAAASUVORK5CYII="


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def ascii_clean(text: str, keep_commas: bool = True) -> str:
    if not text:
        return ""
    text = text.replace("\t", "," if keep_commas else " ")
    out = []
    for ch in text:
        o = ord(ch)
        if ch in "\r\n":
            out.append(ch)
        elif 32 <= o <= 126:
            out.append(ch)
    return "".join(out)


def decode_scanner_bytes(raw: bytes, clean: bool = True) -> str:
    text = raw.decode("latin-1", errors="ignore").strip("\r\n")
    return ascii_clean(text) if clean else text


def first_command_line(text: str, cmd: str) -> str:
    for line in text.splitlines() or [text]:
        line = ascii_clean(line).strip()
        if line.startswith(cmd):
            return line
    for line in text.splitlines() or [text]:
        line = ascii_clean(line).strip()
        if line:
            return line
    return ""


def command_name(line: str) -> str:
    line = (line or "").lstrip()
    if not line:
        return ""
    return line.split(",", 1)[0].strip().upper()


def pretty_xml_from_gsi(line: str) -> str:
    xml_text = UnidenProtocol.extract_xml(line) if "UnidenProtocol" in globals() else ""
    if not xml_text:
        return clean_field(line)
    try:
        root = ET.fromstring(xml_text)
        try:
            ET.indent(root, space="  ")
        except Exception:
            pass
        return ET.tostring(root, encoding="unicode")
    except Exception:
        return xml_text


def clean_field(s: str) -> str:
    return ascii_clean(s or "", keep_commas=False).strip()


def strip_prefix(value: str, prefixes=("TGID", "UID", "U-Id", "U_Id", "UnitID")) -> str:
    value = clean_field(value)
    for p in prefixes:
        value = re.sub(rf"^\s*{re.escape(p)}\s*:\s*", "", value, flags=re.I)
    return value.strip()


def valid_text(value: str) -> bool:
    v = clean_field(value).lower()
    return bool(v and v not in {"none", "uid none", "tgid none", "---", "--", "off", "n/a", "na"})




def format_sts_vertical(line: str) -> str:
    """Format an STS/GST CSV response as a vertical field list.

    STS is COMMAND,DSP_FORM,L1_CHAR,L1_MODE,...,L20_CHAR,L20_MODE,<trailing status fields>.
    This keeps the Raw Output tab as a live static snapshot, but makes the STS
    section readable like ProScan's detailed text view.
    """
    try:
        parts = UnidenProtocol.parse_csv_line(line)
    except Exception:
        return line
    if not parts or parts[0] not in ("STS", "GST"):
        return line

    cmd = parts[0]
    out = [f"{cmd},"]
    if len(parts) > 1:
        out.append(f"  DSP_FORM: {parts[1]}")

    # Up to 20 visible screen rows; each has a CHAR field and a MODE field.
    idx = 2
    row_no = 1
    while idx + 1 < len(parts) and row_no <= 20:
        char = ascii_clean(parts[idx] or "", keep_commas=False)
        mode = ascii_clean(parts[idx + 1] or "", keep_commas=False)
        out.append(f"  L{row_no:02d}_CHAR: {char}")
        out.append(f"  L{row_no:02d}_MODE: {mode}")
        idx += 2
        row_no += 1

    # Anything left after the 20 display rows is scanner status/reserved data.
    rsv_no = 1
    while idx < len(parts):
        out.append(f"  RSV{rsv_no}: {ascii_clean(parts[idx] or '', keep_commas=False)}")
        idx += 1
        rsv_no += 1
    return "\n".join(out)

@dataclass
class ParsedDisplay:
    monitor: str = "---"
    system: str = "---"
    department: str = "---"
    channel: str = "---"
    unit: str = "---"
    detail: str = ""
    sig: int = 0
    scanning: bool = False
    rows: list[str] = field(default_factory=list)


class UnidenProtocol:
    @staticmethod
    def parse_csv_line(line: str):
        line = ascii_clean(line or "")
        return next(csv.reader([line], skipinitialspace=False))

    @staticmethod
    def protocol_for_model(model: str) -> str:
        model = (model or "").upper()
        if "BCD" in model:
            return "BCD"
        return "SDS"

    @staticmethod
    def extract_xml(line: str) -> str:
        text = line or ""
        start = text.find("<?xml")
        if start < 0:
            start = text.find("<ScannerInfo")
        if start < 0:
            return ""
        xml_text = text[start:]
        end = xml_text.find("</ScannerInfo>")
        if end >= 0:
            xml_text = xml_text[: end + len("</ScannerInfo>")]
        return xml_text.strip().rstrip(",")

    @staticmethod
    def sts_screen_rows(line: str, max_rows: int = 40) -> list[str]:
        try:
            parts = UnidenProtocol.parse_csv_line(line)
        except Exception:
            return []
        if not parts or parts[0] not in ("STS", "GST") or len(parts) < 3:
            return []
        rows = []
        i = 2
        while i + 1 < len(parts) and len(rows) < max_rows:
            text = ascii_clean(parts[i] or "", keep_commas=False).strip()
            if text and (any(ch.isalnum() for ch in text) or "_" in text or "*" in text):
                rows.append(text)
            i += 2
        return rows

    @staticmethod
    def summary_from_glg(line: str) -> list[str]:
        parts = UnidenProtocol.parse_csv_line(line)
        if not parts or parts[0] != "GLG":
            return []
        labels = ["Freq/TGID", "Mode", "ATT", "Tone/NAC", "System/Site", "Group", "Channel", "SQL", "Mute", "Sys Tag", "Chan Tag", "P25 NAC/CC"]
        out = []
        for label, val in zip(labels, parts[1:]):
            val = clean_field(val)
            if val:
                out.append(f"{label}: {val}")
        return out or ["No active reception"]

    @staticmethod
    def parse_gsi(line: str) -> dict:
        xml_text = UnidenProtocol.extract_xml(line)
        if not xml_text:
            return {}
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return {}

        def attrs(tag: str) -> dict:
            for el in root.iter():
                if str(el.tag).split("}")[-1] == tag:
                    return dict(el.attrib)
            return {}

        def attr(data: dict, *names: str) -> str:
            lower = {str(k).lower(): v for k, v in (data or {}).items()}
            for name in names:
                v = (data or {}).get(name)
                if v not in (None, ""):
                    return str(v)
                v = lower.get(str(name).lower())
                if v not in (None, ""):
                    return str(v)
            return ""

        monitor = attrs("MonitorList")
        system = attrs("System")
        dept = attrs("Department")
        conv = attrs("ConvFrequency")
        tgid = attrs("TGID")
        unit = attrs("UnitID")
        prop = attrs("Property")
        active = tgid or conv

        unit_name = attr(unit, "Name") or attr(unit, "U_Id", "UID", "UnitID")
        unit_value = strip_prefix(unit_name)
        if not valid_text(unit_value):
            unit_value = "---"

        channel_name = attr(tgid, "Name") or attr(conv, "Name") or attr(tgid, "TGID") or attr(conv, "TGID")
        channel_value = strip_prefix(channel_name, prefixes=("TGID",))
        if not valid_text(channel_value):
            channel_value = "---"

        dept_value = attr(dept, "Name")
        if not valid_text(dept_value):
            dept_value = "---"

        monitor_value = attr(monitor, "Name")
        system_value = attr(system, "Name")

        hold = (attr(active, "Hold") or "").strip().lower()
        if hold in ("on", "hold", "held", "true", "1", "yes"):
            scanning = False
        elif hold in ("off", "false", "0", "no"):
            scanning = True
        else:
            scanning = "scan" in str(root.attrib.get("Mode", "")).lower()

        try:
            sig = max(0, min(5, int(float(attr(prop, "Sig", "Signal") or 0))))
        except Exception:
            sig = 0

        return {
            "monitor": monitor_value or "---",
            "system": system_value or "---",
            "department": dept_value,
            "channel": channel_value,
            "unit": unit_value,
            "sig": sig,
            "scanning": scanning,
        }

    @staticmethod
    def sts_right_detail(rows: list[str], unit_value: str) -> str:
        uid = strip_prefix(unit_value)
        if not valid_text(uid):
            return ""
        # The detail row is the fixed-width STS screen row that repeats the UID and
        # then contains the right-side detail, for example:
        # UID:3299902     STD ADP 290
        for row in rows:
            r = clean_field(row)
            if not r:
                continue
            m = re.match(rf"^\s*(?:UID|U-Id|U_Id)\s*:\s*{re.escape(uid)}\s+(.+?)\s*$", r, flags=re.I)
            if m:
                detail = clean_field(m.group(1))
                if valid_text(detail):
                    return detail
        return ""

    @staticmethod
    def parse_hybrid(sts_line: str, gsi_line: str) -> ParsedDisplay:
        rows = UnidenProtocol.sts_screen_rows(sts_line, 40)
        meta = UnidenProtocol.parse_gsi(gsi_line)
        parsed = ParsedDisplay(rows=rows)
        if meta:
            parsed.monitor = meta.get("monitor", "---")
            parsed.system = meta.get("system", "---")
            parsed.department = meta.get("department", "---")
            parsed.channel = meta.get("channel", "---")
            parsed.unit = meta.get("unit", "---")
            parsed.sig = int(meta.get("sig", 0) or 0)
            parsed.scanning = bool(meta.get("scanning", False))
            parsed.detail = UnidenProtocol.sts_right_detail(rows, parsed.unit)
        return parsed


class ScannerWorkerBase(threading.Thread):
    def __init__(self, mode: str, interval_ms: int, out_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.mode = mode
        self.interval = max(0.1, interval_ms / 1000.0)
        self.out_queue = out_queue
        self.stop_event = stop_event

    def open_connection(self):
        raise NotImplementedError

    def send_command(self, cmd: str) -> str:
        raise NotImplementedError

    def connection_label(self) -> str:
        return "scanner"

    def close_connection(self):
        pass

    def run(self):
        connection_opened = False
        try:
            try:
                self.open_connection()
                connection_opened = True
            except Exception as exc:
                self.out_queue.put(("error", f"Connection failed: {exc}"))
                return

            self.out_queue.put(("status", f"Connected to {self.connection_label()}"))
            for cmd in ("MDL", "VER"):
                if self.stop_event.is_set():
                    return
                try:
                    line = self.send_command(cmd)
                    if line:
                        self.out_queue.put(("raw", line))
                except Exception:
                    pass

            while not self.stop_event.is_set():
                protocol = UnidenProtocol.protocol_for_model(self.mode)
                try:
                    if protocol == "SDS":
                        # SDS uses the richer GSI + STS pair for the live display,
                        # plus a few extra commands for the ProScan-style Raw tab.
                        gsi_line = self.send_command("GSI")
                        sts_line = self.send_command("STS")
                        glg_line = self.send_command("GLG")
                        vol_line = self.send_command("VOL")
                        sql_line = self.send_command("SQL")
                        pwr_line = self.send_command("PWR")
                        for line in (sts_line, glg_line, vol_line, sql_line, pwr_line, gsi_line):
                            if line:
                                self.out_queue.put(("raw", line))
                        if sts_line:
                            self.out_queue.put(("display", UnidenProtocol.parse_hybrid(sts_line, gsi_line)))
                    else:
                        # BCD scanners do not provide SDS-style GSI XML.  Keep one
                        # display mode, but poll the BCD-compatible text/status commands.
                        sts_line = self.send_command("STS")
                        glg_line = self.send_command("GLG")
                        vol_line = self.send_command("VOL")
                        sql_line = self.send_command("SQL")
                        for line in (sts_line, glg_line, vol_line, sql_line):
                            if line:
                                self.out_queue.put(("raw", line))
                        display_rows = UnidenProtocol.sts_screen_rows(sts_line, 40) if sts_line else []
                        glg_rows = UnidenProtocol.summary_from_glg(glg_line) if glg_line else []
                        if display_rows or glg_rows:
                            self.out_queue.put(("text_display", display_rows + ([""] if display_rows and glg_rows else []) + glg_rows))
                        else:
                            self.out_queue.put(("status", "No response; check connection settings and scanner remote mode."))
                except Exception as exc:
                    self.out_queue.put(("error", str(exc)))
                    break
                time.sleep(self.interval)
        finally:
            if connection_opened:
                try:
                    self.close_connection()
                except Exception:
                    pass
            self.out_queue.put(("status", "Disconnected"))


class SerialWorker(ScannerWorkerBase):
    def __init__(self, port: str, baud: str, mode: str, interval_ms: int, out_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(mode, interval_ms, out_queue, stop_event)
        self.port = port
        self.baud = int(baud)
        self.ser = None

    def open_connection(self):
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: python -m pip install pyserial")
        self.ser = serial.Serial(self.port, self.baud, bytesize=8, parity="N", stopbits=1, timeout=1.0)
        time.sleep(0.15)

    def connection_label(self):
        return f"{self.port} @ {self.baud}"

    def send_command(self, cmd: str) -> str:
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode("ascii"))
        self.ser.flush()
        if cmd == "GSI":
            chunks = []
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                raw = self.ser.read_until(b"\r", 8192)
                if raw:
                    chunks.append(decode_scanner_bytes(raw, clean=False))
                    if b"</ScannerInfo>" in raw or "</ScannerInfo>" in chunks[-1]:
                        break
                else:
                    break
            return "".join(chunks)
        raw = self.ser.read_until(b"\r", 4096)
        return first_command_line(decode_scanner_bytes(raw), cmd)

    def close_connection(self):
        if self.ser and self.ser.is_open:
            self.ser.close()


class UdpWorker(ScannerWorkerBase):
    def __init__(self, host: str, port: str, mode: str, interval_ms: int, out_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(mode, interval_ms, out_queue, stop_event)
        self.host = host
        self.port = int(port)
        self.sock = None

    def open_connection(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)

    def connection_label(self):
        return f"{self.host}:{self.port} UDP"

    def send_command(self, cmd: str) -> str:
        self.sock.sendto((cmd + "\r").encode("ascii"), (self.host, self.port))
        chunks = []
        deadline = time.monotonic() + (1.5 if cmd == "GSI" else 1.0)
        while time.monotonic() < deadline:
            try:
                data, _addr = self.sock.recvfrom(8192)
            except socket.timeout:
                break
            if not data:
                break
            text = decode_scanner_bytes(data, clean=(cmd != "GSI"))
            if text:
                chunks.append(text)
            if cmd != "GSI" or "</ScannerInfo>" in text:
                break
        joined = "\n".join(chunks)
        return joined if cmd == "GSI" else first_command_line(joined, cmd)

    def close_connection(self):
        if self.sock:
            self.sock.close()


def px_font(family: str, px: int, bold: bool = False) -> QtGui.QFont:
    """Create a font using exact pixel size for steadier small popout text."""
    font = QtGui.QFont(family)
    font.setPixelSize(int(px))
    font.setBold(bool(bold))
    try:
        font.setHintingPreference(QtGui.QFont.PreferFullHinting)
    except Exception:
        pass
    return font


class SignalBars(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = 0
        self.active_color = QtGui.QColor("#f4f4f4")
        self.inactive_color = QtGui.QColor("#555b63")
        self.setFixedSize(30, 18)

    def set_colors(self, active: str, inactive: str):
        self.active_color = QtGui.QColor(active)
        self.inactive_color = QtGui.QColor(inactive)
        self.update()

    def set_level(self, level: int):
        self.level = max(0, min(5, int(level or 0)))
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        bar_w, gap, base_y = 2, 3, 16
        heights = [4, 7, 10, 13, 16]
        x0 = 4
        for i, h in enumerate(heights, start=1):
            color = self.active_color if i <= self.level else self.inactive_color
            painter.fillRect(QtCore.QRect(x0 + (i - 1) * (bar_w + gap), base_y - h, bar_w + 1, h), color)


class PopoutDisplay(QtWidgets.QWidget):
    THEMES = {
        "night": {
            "bg": "#202327",
            "fg": "#f4f4f4",
            "border": "#050505",
            "sig_inactive": "#555b63",
        },
        "day": {
            "bg": "#f3f4f1",
            "fg": "#111111",
            "border": "#c9ccc7",
            "sig_inactive": "#b7bab6",
        },
    }

    def __init__(self, parent=None, theme: str = "night"):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Window)
        self.setObjectName("popoutDisplay")
        self.setWindowTitle("Uniden Display")
        self.setFixedSize(520, 185)
        self._drag_offset = None
        self.theme = "night"

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(0)

        top_line = QtWidgets.QHBoxLayout()
        top_line.setContentsMargins(0, 0, 0, 0)
        top_line.setSpacing(6)
        self.topLabel = QtWidgets.QLabel("--- | ---")
        self.topLabel.setObjectName("popoutTopLabel")
        self.topLabel.setFont(px_font("Roboto", 14))
        top_line.addWidget(self.topLabel, 1)

        self.scanLabel = QtWidgets.QLabel()
        self.scanLabel.setFixedSize(36, 18)
        pm = QtGui.QPixmap()
        pm.loadFromData(QtCore.QByteArray.fromBase64(SCAN_ICON_PNG_B64.encode("ascii")), "PNG")
        self.scanPixmap = pm
        self.scanLabel.setAlignment(QtCore.Qt.AlignCenter)
        top_line.addWidget(self.scanLabel, 0, QtCore.Qt.AlignVCenter)

        self.sigBars = SignalBars()
        top_line.addWidget(self.sigBars, 0, QtCore.Qt.AlignVCenter)
        outer.addLayout(top_line)
        outer.addSpacing(10)

        self.departmentLabel = QtWidgets.QLabel("---")
        self.departmentLabel.setAlignment(QtCore.Qt.AlignCenter)
        self.departmentLabel.setFont(px_font("Roboto", 18))
        outer.addWidget(self.departmentLabel)

        self.channelLabel = QtWidgets.QLabel("---")
        self.channelLabel.setAlignment(QtCore.Qt.AlignCenter)
        self.channelLabel.setFont(px_font("Roboto", 24))
        outer.addWidget(self.channelLabel)

        self.unitLabel = QtWidgets.QLabel("---")
        self.unitLabel.setAlignment(QtCore.Qt.AlignCenter)
        self.unitLabel.setFont(px_font("Roboto", 16))
        outer.addWidget(self.unitLabel)

        outer.addSpacing(20)
        self.detailLabel = QtWidgets.QLabel("")
        self.detailLabel.setAlignment(QtCore.Qt.AlignCenter)
        self.detailLabel.setFont(px_font("Roboto", 12))
        outer.addWidget(self.detailLabel)
        outer.addStretch(1)

        self.apply_theme(theme)

    def _tinted_scan_pixmap(self, color: str) -> QtGui.QPixmap:
        if self.scanPixmap.isNull():
            return self.scanPixmap
        tinted = QtGui.QPixmap(self.scanPixmap.size())
        tinted.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(tinted)
        painter.drawPixmap(0, 0, self.scanPixmap)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), QtGui.QColor(color))
        painter.end()
        return tinted

    def apply_theme(self, theme: str):
        self.theme = "day" if str(theme).lower() == "day" else "night"
        colors = self.THEMES[self.theme]
        bg = colors["bg"]
        fg = colors["fg"]
        border = colors["border"]
        self.setStyleSheet(f"""
            QWidget#popoutDisplay {{ background: {bg}; border: 1px solid {border}; }}
            QLabel {{ color: {fg}; background: transparent; }}
        """)
        self.scanLabel.setPixmap(self._tinted_scan_pixmap(fg))
        self.sigBars.set_colors(fg, colors["sig_inactive"])

    def update_display(self, parsed: ParsedDisplay):
        self.topLabel.setText(f"{parsed.monitor or '---'} | {parsed.system or '---'}")
        self.departmentLabel.setText(parsed.department or "---")
        self.channelLabel.setText(parsed.channel or "---")
        self.unitLabel.setText(parsed.unit or "---")
        self.detailLabel.setText(parsed.detail if valid_text(parsed.detail) else "")
        self.sigBars.set_level(parsed.sig)
        self.scanLabel.setVisible(parsed.scanning)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event):
        self.close()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        loader = QUiLoader()
        ui_path = resource_path("uniden_text_display.ui")
        ui_file = QtCore.QFile(ui_path)
        if not ui_file.open(QtCore.QFile.ReadOnly):
            raise RuntimeError(f"Could not open UI file: {ui_path}")
        self.ui = loader.load(ui_file, self)
        ui_file.close()
        self.setCentralWidget(self.ui)
        self.setWindowTitle("Uniden Scanner Text Display - Qt")

        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.popout: PopoutDisplay | None = None
        self.last_parsed = ParsedDisplay()
        self.popout_theme = "night"
        self.latest_raw: dict[str, str] = {}
        self.raw_order = ["MDL", "VER", "STS", "GLG", "VOL", "SQL", "PWR", "GSI"]

        self._bind_widgets()
        self._init_controls()
        self._connect_signals()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.process_queue)
        self.timer.start(100)

    def w(self, name):
        return self.ui.findChild(QtCore.QObject, name)

    def _bind_widgets(self):
        self.connectionModeCombo = self.w("connectionModeCombo")
        self.comPortCombo = self.w("comPortCombo")
        self.refreshPortsButton = self.w("refreshPortsButton")
        self.baudCombo = self.w("baudCombo")
        self.hostEdit = self.w("hostEdit")
        self.udpPortEdit = self.w("udpPortEdit")
        self.modelCombo = self.w("modelCombo")
        self.pollSpin = self.w("pollSpin")
        self.connectButton = self.w("connectButton")
        self.disconnectButton = self.w("disconnectButton")
        self.openPopoutButton = self.w("openPopoutButton")
        self.closePopoutButton = self.w("closePopoutButton")
        self.dayModeButton = self.w("dayModeButton")
        self.nightModeButton = self.w("nightModeButton")
        self.statusLabel = self.w("statusLabel")
        self.displayText = self.w("displayText")
        self.rawText = self.w("rawText")
        self.clearRawButton = self.w("clearRawButton")

    def _init_controls(self):
        self.connectionModeCombo.addItems(["Serial / USB COM", "SDS200 Network UDP"])
        self.baudCombo.addItems(BAUD_CHOICES)
        self.baudCombo.setCurrentText("115200")
        self.modelCombo.addItems(MODEL_CHOICES)
        self.modelCombo.setCurrentIndex(0)
        self.udpPortEdit.setText("50536")
        self.pollSpin.setValue(500)
        self.refresh_ports()
        self.update_connection_fields()
        self.disconnectButton.setEnabled(False)
        self.statusLabel.setText("Not connected")

    def _connect_signals(self):
        self.connectionModeCombo.currentTextChanged.connect(self.update_connection_fields)
        self.refreshPortsButton.clicked.connect(self.refresh_ports)
        self.connectButton.clicked.connect(self.connect_scanner)
        self.disconnectButton.clicked.connect(self.disconnect_scanner)
        self.openPopoutButton.clicked.connect(self.open_popout)
        self.closePopoutButton.clicked.connect(self.close_popout)
        if self.dayModeButton is not None:
            self.dayModeButton.clicked.connect(lambda: self.set_popout_theme("day"))
        if self.nightModeButton is not None:
            self.nightModeButton.clicked.connect(lambda: self.set_popout_theme("night"))
        self.clearRawButton.clicked.connect(self.clear_raw_snapshot)

    def _set_display_text(self, lines: list[str]):
        """Update the main Display tab with the full STS screen text view."""
        if self.displayText is not None:
            self.displayText.setPlainText("\n".join(lines or []))

    def refresh_ports(self):
        current = self.comPortCombo.currentText()
        self.comPortCombo.clear()
        ports = []
        if list_ports is not None:
            ports = [p.device for p in list_ports.comports()]
        if not ports:
            ports = ["COM1", "COM2", "COM3", "COM4"]
        self.comPortCombo.addItems(ports)
        if current:
            idx = self.comPortCombo.findText(current)
            if idx >= 0:
                self.comPortCombo.setCurrentIndex(idx)

    def update_connection_fields(self):
        network = "Network" in self.connectionModeCombo.currentText()
        self.comPortCombo.setEnabled(not network)
        self.baudCombo.setEnabled(not network)
        self.refreshPortsButton.setEnabled(not network)
        self.hostEdit.setEnabled(network)
        self.udpPortEdit.setEnabled(network)

    def connect_scanner(self):
        if self.worker is not None:
            return
        self.stop_event.clear()
        mode = self.modelCombo.currentText()
        interval = self.pollSpin.value()
        try:
            if "Network" in self.connectionModeCombo.currentText():
                host = self.hostEdit.text().strip()
                port = self.udpPortEdit.text().strip() or "50536"
                if not host:
                    QtWidgets.QMessageBox.warning(self, "Missing IP", "Enter the scanner IP address.")
                    return
                self.worker = UdpWorker(host, port, mode, interval, self.queue, self.stop_event)
            else:
                port = self.comPortCombo.currentText().strip()
                baud = self.baudCombo.currentText().strip()
                self.worker = SerialWorker(port, baud, mode, interval, self.queue, self.stop_event)
            self.worker.start()
            self.connectButton.setEnabled(False)
            self.disconnectButton.setEnabled(True)
            self.statusLabel.setText("Connecting...")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Connection error", str(exc))
            self.worker = None

    def disconnect_scanner(self):
        self.stop_event.set()
        self.worker = None
        self.connectButton.setEnabled(True)
        self.disconnectButton.setEnabled(False)

    def set_popout_theme(self, theme: str):
        self.popout_theme = "day" if str(theme).lower() == "day" else "night"
        if self.popout is not None:
            self.popout.apply_theme(self.popout_theme)

    def open_popout(self):
        if self.popout is None:
            self.popout = PopoutDisplay(self, theme=self.popout_theme)
            self.popout.destroyed.connect(lambda: setattr(self, "popout", None))
        else:
            self.popout.apply_theme(self.popout_theme)
        self.popout.update_display(self.last_parsed)
        self.popout.show()
        self.popout.raise_()

    def close_popout(self):
        if self.popout is not None:
            self.popout.close()
            self.popout = None

    def process_queue(self):
        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "raw":
                self.update_raw_snapshot(str(payload))
            elif kind == "status":
                self.statusLabel.setText(str(payload))
                if str(payload).startswith("Disconnected"):
                    self.connectButton.setEnabled(True)
                    self.disconnectButton.setEnabled(False)
                    self.worker = None
            elif kind == "error":
                self.statusLabel.setText(f"Error: {payload}")
                self.rawText.appendPlainText(f"ERROR: {payload}")
                self.connectButton.setEnabled(True)
                self.disconnectButton.setEnabled(False)
                self.worker = None
            elif kind == "display":
                self.set_parsed_display(payload)
            elif kind == "text_display":
                self._set_display_text(payload or [])

    def clear_raw_snapshot(self):
        self.latest_raw.clear()
        self.rawText.clear()

    def update_raw_snapshot(self, line: str):
        cmd = command_name(line)
        if not cmd:
            return
        self.latest_raw[cmd] = line

        # The Raw Output tab is a live static snapshot. Updating the full text
        # with setPlainText() resets QPlainTextEdit's scroll position, which
        # makes the view jump while the user is trying to inspect lower STS/GSI
        # fields. Preserve the current vertical/horizontal scroll positions
        # across refreshes so the user can scroll normally while data updates.
        vbar = self.rawText.verticalScrollBar()
        hbar = self.rawText.horizontalScrollBar()
        vpos = vbar.value()
        hpos = hbar.value()
        self.rawText.setPlainText(self.format_raw_snapshot())
        vbar.setValue(min(vpos, vbar.maximum()))
        hbar.setValue(min(hpos, hbar.maximum()))

    def format_raw_snapshot(self) -> str:
        out: list[str] = []
        for cmd in self.raw_order:
            line = self.latest_raw.get(cmd, "")
            if not line:
                continue
            if cmd == "VER":
                # Keep VER available internally but omit it from the ProScan-like
                # static snapshot unless you want to add it back later.
                continue
            if cmd == "GSI":
                out.append("GSI,<XML>,")
                out.append(pretty_xml_from_gsi(line))
            elif cmd in ("STS", "GST"):
                out.append(format_sts_vertical(line))
            else:
                out.append(line)
        return "\n".join(out)

    def set_parsed_display(self, parsed: ParsedDisplay):
        self.last_parsed = parsed
        # Main Display tab: full scanner-screen STS text.
        # Borderless popout: compact radio-style view.
        lines = parsed.rows or []
        self._set_display_text(lines if lines else ["(blank / no active STS display text)"])
        if self.popout is not None:
            self.popout.update_display(parsed)

    def closeEvent(self, event):
        self.disconnect_scanner()
        self.close_popout()
        super().closeEvent(event)


def load_app_fonts():
    """Load bundled Roboto fonts when present next to the script/EXE.

    Font files are not required for development if Roboto is already installed.
    For a portable EXE, place Roboto-Regular.ttf and Roboto-Bold.ttf beside
    this script and include them with PyInstaller --add-data.
    """
    for font_file in ("Roboto-Regular.ttf", "Roboto-Bold.ttf"):
        path = resource_path(font_file)
        if os.path.exists(path):
            QtGui.QFontDatabase.addApplicationFont(path)


def main():
    app = QtWidgets.QApplication(sys.argv)
    load_app_fonts()
    app.setApplicationName("Uniden Scanner Text Display")
    win = MainWindow()
    win.resize(820, 620)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
