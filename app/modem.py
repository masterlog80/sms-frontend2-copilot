"""
Modem AT-command interface for HSDPA USB STICK SIM Modem (3G/7.2 Mbps).
Communicates with the device via /dev/ttyUSB0 using AT commands over serial.
"""
import re
import time
import threading
import logging
from datetime import datetime

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

logger = logging.getLogger(__name__)


class ModemError(Exception):
    pass


class ModemManager:
    """Manages serial communication with a USB GSM/HSDPA modem."""

    def __init__(self, device="/dev/ttyUSB0", baudrate=115200, timeout=5):
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None
        self._lock = threading.Lock()
        self.connected = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Open the serial port and initialise the modem."""
        if not SERIAL_AVAILABLE:
            logger.error("pyserial is not installed")
            return False
        try:
            self._serial = serial.Serial(
                self.device,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
            )
            time.sleep(0.5)
            self._flush()
            # basic handshake
            self._cmd("AT")
            # disable echo so parsing is easier
            self._cmd("ATE0")
            # switch to text-mode SMS
            self._cmd("AT+CMGF=1")
            # use SIM card storage for SMS
            self._cmd('AT+CPMS="SM","SM","SM"')
            self.connected = True
            logger.info("Modem connected on %s", self.device)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Cannot connect to modem: %s", exc)
            self.connected = False
            return False

    def disconnect(self):
        """Close the serial port."""
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception:  # noqa: BLE001
            pass
        self.connected = False

    def reconnect(self) -> bool:
        """Attempt a reconnect."""
        self.disconnect()
        time.sleep(1)
        return self.connect()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _flush(self):
        if self._serial and self._serial.is_open:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()

    def _cmd(self, command: str, delay: float = 0.3) -> str:
        """Send an AT command and return the raw response string."""
        if not self._serial or not self._serial.is_open:
            raise ModemError("Serial port not open")
        raw = (command + "\r\n").encode()
        self._serial.write(raw)
        time.sleep(delay)
        response = b""
        while self._serial.in_waiting:
            response += self._serial.read(self._serial.in_waiting)
            time.sleep(0.05)
        decoded = response.decode("utf-8", errors="replace").strip()
        logger.debug("CMD %s -> %s", command, decoded)
        return decoded

    # ------------------------------------------------------------------
    # Public modem queries
    # ------------------------------------------------------------------

    def get_signal_strength(self) -> dict:
        """
        Query AT+CSQ and return signal info.
        Returns dict with keys: rssi, ber, dbm, percent, quality.
        """
        with self._lock:
            try:
                resp = self._cmd("AT+CSQ")
                m = re.search(r"\+CSQ:\s*(\d+),(\d+)", resp)
                if not m:
                    return {"rssi": 99, "ber": 99, "dbm": None, "percent": 0, "quality": "Unknown"}
                rssi = int(m.group(1))
                ber = int(m.group(2))
                if rssi == 99:
                    dbm = None
                    percent = 0
                    quality = "Unknown"
                else:
                    dbm = -113 + 2 * rssi
                    # rssi=0 → -113 dBm (0 %), rssi=31 → -51 dBm (100 %)
                    percent = min(100, max(0, int((rssi / 31) * 100)))
                    if rssi >= 20:
                        quality = "Excellent"
                    elif rssi >= 15:
                        quality = "Good"
                    elif rssi >= 10:
                        quality = "Fair"
                    elif rssi >= 5:
                        quality = "Poor"
                    else:
                        quality = "Very Poor"
                return {
                    "rssi": rssi,
                    "ber": ber,
                    "dbm": dbm,
                    "percent": percent,
                    "quality": quality,
                }
            except Exception as exc:  # noqa: BLE001
                logger.error("get_signal_strength error: %s", exc)
                self.connected = False
                return {"rssi": 99, "ber": 99, "dbm": None, "percent": 0, "quality": "Error"}

    def get_memory(self) -> dict:
        """
        Query AT+CPMS? and return SMS memory info.
        Returns dict with keys: used, total, free, percent_used.
        """
        with self._lock:
            try:
                resp = self._cmd("AT+CPMS?")
                # +CPMS: "SM",3,20,"SM",3,20,"SM",3,20
                m = re.search(r"\+CPMS:\s*\S+,(\d+),(\d+)", resp)
                if not m:
                    return {"used": 0, "total": 0, "free": 0, "percent_used": 0}
                used = int(m.group(1))
                total = int(m.group(2))
                free = total - used
                percent_used = int((used / total) * 100) if total else 0
                return {
                    "used": used,
                    "total": total,
                    "free": free,
                    "percent_used": percent_used,
                }
            except Exception as exc:  # noqa: BLE001
                logger.error("get_memory error: %s", exc)
                self.connected = False
                return {"used": 0, "total": 0, "free": 0, "percent_used": 0}

    def list_sms(self) -> list:
        """
        Return all SMS messages stored on the SIM as a list of dicts.
        Each dict has: index, status, sender, timestamp, message.
        """
        with self._lock:
            try:
                # Longer delay needed to receive all messages
                resp = self._cmd('AT+CMGL="ALL"', delay=1.5)
                return self._parse_sms_list(resp)
            except Exception as exc:  # noqa: BLE001
                logger.error("list_sms error: %s", exc)
                self.connected = False
                return []

    def delete_sms(self, index: int) -> bool:
        """Delete an SMS by its modem index."""
        with self._lock:
            try:
                resp = self._cmd(f"AT+CMGD={index}")
                return "OK" in resp
            except Exception as exc:  # noqa: BLE001
                logger.error("delete_sms error: %s", exc)
                return False

    def get_modem_info(self) -> dict:
        """Return manufacturer, model and IMEI."""
        with self._lock:
            info = {}
            try:
                info["manufacturer"] = self._cmd("AT+CGMI").split("\n")[0].strip()
                info["model"] = self._cmd("AT+CGMM").split("\n")[0].strip()
                m = re.search(r"\d{15}", self._cmd("AT+CGSN"))
                info["imei"] = m.group(0) if m else "Unknown"
                m2 = re.search(r"\+CREG:\s*\d,(\d)", self._cmd("AT+CREG?"))
                status_map = {
                    "0": "Not registered",
                    "1": "Registered (Home)",
                    "2": "Searching",
                    "3": "Registration denied",
                    "5": "Registered (Roaming)",
                }
                info["network_status"] = status_map.get(m2.group(1) if m2 else "0", "Unknown")
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_modem_info partial error: %s", exc)
            return info

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sms_list(raw: str) -> list:
        """Parse the AT+CMGL="ALL" response into a list of message dicts."""
        messages = []
        # Split on message header lines
        lines = raw.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # Header pattern: +CMGL: 1,"REC UNREAD","+1234567890",,"21/01/01,12:00:00+00"
            m = re.match(
                r'\+CMGL:\s*(\d+),"([^"]+)","([^"]*)"[^,]*,(?:"([^"]*)")?',
                line,
            )
            if m:
                idx = int(m.group(1))
                status = m.group(2)
                sender = m.group(3)
                timestamp_raw = m.group(4) or ""
                # collect body lines until next header or end
                body_lines = []
                i += 1
                while i < len(lines):
                    next_line = lines[i].strip()
                    if re.match(r"\+CMGL:", next_line) or next_line in ("OK", "ERROR"):
                        break
                    body_lines.append(next_line)
                    i += 1
                body = " ".join(body_lines).strip()
                # Parse timestamp
                ts = None
                try:
                    # Format: YY/MM/DD,HH:MM:SS+TZ
                    ts_clean = re.sub(r"[+\-]\d+$", "", timestamp_raw)
                    ts = datetime.strptime(ts_clean, "%y/%m/%d,%H:%M:%S").isoformat()
                except Exception:  # noqa: BLE001
                    ts = timestamp_raw
                messages.append(
                    {
                        "index": idx,
                        "status": status,
                        "sender": sender,
                        "timestamp": ts,
                        "message": body,
                    }
                )
            else:
                i += 1
        return messages
