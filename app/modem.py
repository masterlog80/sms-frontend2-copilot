"""
Modem AT-command interface for HSDPA USB STICK SIM Modem (3G/7.2 Mbps).
Communicates with the device via /dev/ttyUSB0 using AT commands over serial.
"""
import re
import time
import threading
import logging
from datetime import datetime, timezone

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

    # GSM-7 basic character table (3GPP TS 23.038)
    _GSM7_BASIC = (
        '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?'
        '¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ`¿abcdefghijklmnopqrstuvwxyzäöñüà'
    )
    # GSM-7 extension table (preceded by 0x1B escape character)
    _GSM7_EXT: dict = {
        0x0A: '\f', 0x14: '^', 0x28: '{', 0x29: '}', 0x2F: '\\',
        0x3C: '[',  0x3D: '~', 0x3E: ']', 0x40: '|', 0x65: '€',
    }

    def __init__(self, device="/dev/ttyUSB0", baudrate=115200, timeout=5):
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None
        self._lock = threading.Lock()
        self.connected = False
        # Optional callback invoked after every AT command exchange.
        # Signature: raw_log_callback(timestamp: str, command: str, response: str)
        self.raw_log_callback = None

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
        if self.raw_log_callback is not None:
            try:
                self.raw_log_callback(
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
                    command,
                    decoded,
                )
            except Exception:  # noqa: BLE001
                pass
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

        Reads in PDU mode (AT+CMGF=0) so that the User Data Header (UDH)
        present in multipart (concatenated) SMS is available for reassembly.
        Text mode is restored via AT+CMGF=1 before the method returns.
        """
        with self._lock:
            try:
                self._cmd("AT+CMGF=0")
                # 4 = list ALL messages; longer delay to receive everything
                resp = self._cmd("AT+CMGL=4", delay=1.5)
                return self._parse_pdu_sms_list(resp)
            except Exception as exc:  # noqa: BLE001
                logger.error("list_sms error: %s", exc)
                self.connected = False
                return []
            finally:
                try:
                    self._cmd("AT+CMGF=1")
                except Exception:  # noqa: BLE001
                    pass

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
                net = self._parse_current_network(self._cmd("AT+COPS?"))
                info["network_name"] = net.get("operator") or "Unknown"
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_modem_info partial error: %s", exc)
            return info

    def get_current_network(self) -> dict:
        """
        Query AT+COPS? and return the currently selected operator.
        Returns dict with keys: mode, format, operator, tech.
        """
        with self._lock:
            try:
                resp = self._cmd("AT+COPS?")
                return self._parse_current_network(resp)
            except Exception as exc:  # noqa: BLE001
                logger.error("get_current_network error: %s", exc)
                return {}

    def scan_networks(self) -> list:
        """
        Scan for available networks using AT+COPS=?.
        Returns a list of dicts with keys: status, long_name, short_name, numeric, tech.
        WARNING: This command can take up to 60 seconds to complete.
        """
        with self._lock:
            try:
                resp = self._cmd("AT+COPS=?", delay=60)
                return self._parse_network_list(resp)
            except Exception as exc:  # noqa: BLE001
                logger.error("scan_networks error: %s", exc)
                return []

    def select_network(self, mode: str, numeric: str = None) -> bool:
        """
        Select a network operator.
        mode: 'auto' sets automatic selection (AT+COPS=0).
        mode: 'manual' selects the network identified by *numeric* (AT+COPS=1,2,"<numeric>").
        Returns True on success.
        """
        with self._lock:
            try:
                if mode == "auto":
                    resp = self._cmd("AT+COPS=0", delay=10)
                else:
                    resp = self._cmd(f'AT+COPS=1,2,"{numeric}"', delay=10)
                return "OK" in resp
            except Exception as exc:  # noqa: BLE001
                logger.error("select_network error: %s", exc)
                return False

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_gsm7(data: bytes, num_septets: int, fill_bits: int = 0) -> str:
        """Decode GSM-7 packed bytes into a Unicode string.

        *fill_bits* is the number of padding bits at the start of the first
        byte (used when a User Data Header precedes the message body).
        """
        result = []
        escape = False
        for i in range(num_septets):
            bit_pos = fill_bits + i * 7
            byte_idx = bit_pos // 8
            bit_off = bit_pos % 8
            if byte_idx >= len(data):
                break
            val = (data[byte_idx] >> bit_off) & 0x7F
            bits_in_first = 8 - bit_off
            if bits_in_first < 7 and byte_idx + 1 < len(data):
                val |= (data[byte_idx + 1] << bits_in_first) & 0x7F
            if escape:
                result.append(ModemManager._GSM7_EXT.get(val, chr(val)))
                escape = False
            elif val == 0x1B:
                escape = True
            else:
                result.append(
                    ModemManager._GSM7_BASIC[val]
                    if val < len(ModemManager._GSM7_BASIC)
                    else "?"
                )
        return "".join(result)

    @staticmethod
    def _parse_pdu_timestamp(scts: bytes) -> str:
        """Decode a 7-byte Service Centre Time Stamp into an ISO-8601 string."""
        def bcd(b: int) -> int:
            return (b & 0x0F) * 10 + ((b >> 4) & 0x0F)
        try:
            year   = bcd(scts[0]) + 2000
            month  = bcd(scts[1])
            day    = bcd(scts[2])
            hour   = bcd(scts[3])
            minute = bcd(scts[4])
            second = bcd(scts[5])
            return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _parse_pdu(pdu_hex: str, idx: int, status: str) -> dict | None:
        """Parse one SMS-DELIVER PDU (hex string) into a message dict.

        Returns a dict with keys: index, status, sender, timestamp, message.
        When the message is part of a concatenated sequence the dict also
        carries concat_ref, concat_total, and concat_part.

        Returns None when the PDU cannot be parsed or is not a supported type.
        """
        try:
            data = bytes.fromhex(pdu_hex.strip())
            pos = 0

            # --- SCA (Service Centre Address) ---
            sca_len = data[pos]
            pos += 1 + sca_len

            # --- First Octet ---
            first_octet = data[pos]
            pos += 1
            mti  = first_octet & 0x03        # Message Type Indicator
            udhi = bool(first_octet & 0x40)  # User Data Header Indicator

            # SMS-DELIVER = MTI 0x00; SMS-STATUS-REPORT = MTI 0x02 (skip)
            if mti == 0x02:
                return None

            # SMS-SUBMIT (MTI 01) has an extra TP-MR byte; skip it
            if mti == 0x01:
                pos += 1  # TP-MR

            # --- Address (OA for DELIVER, DA for SUBMIT) ---
            addr_len_nibbles = data[pos]
            pos += 1
            addr_ton_npi = data[pos]
            pos += 1
            addr_byte_len = (addr_len_nibbles + 1) // 2
            addr_bytes = data[pos:pos + addr_byte_len]
            pos += addr_byte_len

            ton = (addr_ton_npi >> 4) & 0x07
            if ton == 5:  # Alphanumeric (GSM-7 encoded)
                num_chars = (addr_len_nibbles * 4) // 7
                sender = ModemManager._decode_gsm7(addr_bytes, num_chars)
            elif ton == 1:  # International
                digits = ""
                for byte in addr_bytes:
                    digits += str(byte & 0x0F)
                    if (byte >> 4) != 0x0F:
                        digits += str((byte >> 4) & 0x0F)
                sender = "+" + digits
            else:
                digits = ""
                for byte in addr_bytes:
                    digits += str(byte & 0x0F)
                    if (byte >> 4) != 0x0F:
                        digits += str((byte >> 4) & 0x0F)
                sender = digits

            # --- TP-PID ---
            pos += 1

            # --- TP-DCS (Data Coding Scheme) ---
            dcs = data[pos]
            pos += 1

            # --- TP-SCTS (SMS-DELIVER) or TP-VP (SMS-SUBMIT) ---
            if mti == 0x00:
                # SMS-DELIVER: fixed 7-byte timestamp
                scts = data[pos:pos + 7]
                pos += 7
                timestamp = ModemManager._parse_pdu_timestamp(scts)
            else:
                # SMS-SUBMIT: variable Validity Period; skip it
                timestamp = ""
                vpf = (first_octet >> 3) & 0x03
                if vpf == 0x02:    # relative VP – 1 byte
                    pos += 1
                elif vpf in (0x01, 0x03):  # enhanced or absolute VP – 7 bytes
                    pos += 7

            # --- TP-UDL ---
            udl = data[pos]
            pos += 1

            # --- TP-UD ---
            ud = data[pos:]

            # Determine character encoding from DCS
            dcs_group = (dcs >> 4) & 0x0F
            if dcs_group in (0x0, 0x1, 0x2, 0x3):
                charset = (dcs >> 2) & 0x03   # 0=GSM-7, 1=8-bit, 2=UCS2
            elif dcs_group == 0xF:
                charset = 0x01 if (dcs & 0x04) else 0x00
            else:
                charset = 0x00  # default to GSM-7

            # --- User Data Header ---
            concat_info = None
            udh_len_bytes = 0
            if udhi and ud:
                udhl = ud[0]                    # bytes that follow the length field
                udh_len_bytes = 1 + udhl        # total bytes consumed by UDH
                udh_data = ud[1:1 + udhl]
                j = 0
                while j + 1 < len(udh_data):
                    iei = udh_data[j]
                    iel = udh_data[j + 1]
                    ie_data = udh_data[j + 2:j + 2 + iel]
                    j += 2 + iel
                    if iei == 0x00 and iel == 0x03 and len(ie_data) == 3:
                        # 8-bit concatenation reference
                        concat_info = {
                            "ref":   ie_data[0],
                            "total": ie_data[1],
                            "part":  ie_data[2],
                        }
                        break
                    if iei == 0x08 and iel == 0x04 and len(ie_data) == 4:
                        # 16-bit concatenation reference
                        concat_info = {
                            "ref":   (ie_data[0] << 8) | ie_data[1],
                            "total": ie_data[2],
                            "part":  ie_data[3],
                        }
                        break

            # --- Decode message body ---
            if charset == 0x00:  # GSM-7
                if udhi:
                    # When a UDH is present, the UDL counts total septets
                    # including pseudo-septets consumed by the UDH.  Padding
                    # bits align the first message septet to a 7-bit boundary
                    # after the UDH octets.
                    fill_bits = (7 - (udh_len_bytes * 8) % 7) % 7
                    udh_septets = (udh_len_bytes * 8 + fill_bits) // 7
                    num_septets = udl - udh_septets
                    body = ModemManager._decode_gsm7(
                        ud[udh_len_bytes:], num_septets, fill_bits
                    )
                else:
                    body = ModemManager._decode_gsm7(ud, udl)
            elif charset == 0x02:  # UCS2
                body = ud[udh_len_bytes:].decode("utf-16-be", errors="replace")
            else:  # 8-bit data
                body = ud[udh_len_bytes:].decode("latin-1", errors="replace")

            entry: dict = {
                "index":     idx,
                "status":    status,
                "sender":    sender,
                "timestamp": timestamp,
                "message":   body,
            }
            if concat_info:
                entry["concat_ref"]   = concat_info["ref"]
                entry["concat_total"] = concat_info["total"]
                entry["concat_part"]  = concat_info["part"]
            return entry

        except Exception as exc:  # noqa: BLE001
            logger.error("PDU parse error (index=%d): %s", idx, exc)
            return None

    @staticmethod
    def _parse_pdu_sms_list(raw: str) -> list:
        """Parse the AT+CMGL=4 response (PDU mode) into a list of message dicts."""
        messages = []
        status_map = {0: "REC UNREAD", 1: "REC READ", 2: "STO UNSENT", 3: "STO SENT"}
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        i = 0
        while i < len(lines):
            # PDU mode response format: +CMGL: <index>,<stat>,,<length>
            # The third (alpha) field is always empty in PDU mode, hence the
            # two consecutive commas.
            m = re.match(r"\+CMGL:\s*(\d+),(\d+),,\d*", lines[i])
            if m:
                idx       = int(m.group(1))
                stat_code = int(m.group(2))
                status    = status_map.get(stat_code, "UNKNOWN")
                i += 1
                if i < len(lines) and lines[i] not in ("OK", "ERROR"):
                    parsed = ModemManager._parse_pdu(lines[i], idx, status)
                    if parsed is not None:
                        messages.append(parsed)
                    i += 1
            else:
                i += 1
        return messages

    @staticmethod
    def _parse_current_network(raw: str) -> dict:
        """Parse AT+COPS? response into a dict."""
        mode_map = {"0": "auto", "1": "manual", "2": "deregister", "4": "manual_auto"}
        tech_map = {
            "0": "GSM", "1": "GSM Compact", "2": "UTRAN (3G)",
            "3": "GSM/EGPRS", "4": "UTRAN/HSDPA", "5": "UTRAN/HSUPA",
            "6": "UTRAN/HSPA", "7": "LTE",
        }
        m = re.search(r"\+COPS:\s*(\d+)(?:,(\d+),\"([^\"]*?)\"(?:,(\d+))?)?", raw)
        if not m:
            return {}
        return {
            "mode": mode_map.get(m.group(1), "unknown"),
            "format": m.group(2) or "",
            "operator": m.group(3) or "",
            "tech": tech_map.get(m.group(4) or "", ""),
        }

    @staticmethod
    def _parse_network_list(raw: str) -> list:
        """Parse AT+COPS=? response into a list of network dicts."""
        tech_map = {
            "0": "GSM", "1": "GSM Compact", "2": "UTRAN (3G)",
            "3": "GSM/EGPRS", "4": "UTRAN/HSDPA", "5": "UTRAN/HSUPA",
            "6": "UTRAN/HSPA", "7": "LTE",
        }
        status_map = {"0": "unknown", "1": "available", "2": "current", "3": "forbidden"}
        networks = []
        for m in re.finditer(
            r'\((\d+),"([^"]*?)","([^"]*?)","(\d+)"(?:,(\d+))?\)', raw
        ):
            tech_code = m.group(5) or ""
            networks.append({
                "status": status_map.get(m.group(1), "unknown"),
                "long_name": m.group(2),
                "short_name": m.group(3),
                "numeric": m.group(4),
                "tech": tech_map.get(tech_code, "Unknown") if tech_code else "Unknown",
            })
        return networks

    @staticmethod
    def _is_ucs2_hex(s: str) -> bool:
        """Return True if *s* looks like a UCS2 (UTF-16 BE) hex-encoded body.

        The check requires:
        - All characters are valid hex digits.
        - The length is a multiple of 4 (one UTF-16 code unit = 2 bytes = 4 hex chars).
        - At least one code unit has a zero high byte (0x00XX), which is the
          normal pattern for Basic-Latin text encoded in UCS2 and distinguishes
          it from a coincidentally all-hex GSM-7 message body.
        """
        if not s or len(s) % 4 != 0:
            return False
        if not re.match(r'^[0-9A-Fa-f]+$', s):
            return False
        # Inspect high bytes (every other 2-char group starting at offset 0).
        high_bytes = [s[i:i + 2] for i in range(0, len(s), 4)]
        return any(b.upper() == '00' for b in high_bytes)

    @staticmethod
    def _parse_udh_concat(hex_body: str):
        """Parse a concatenated-SMS User Data Header embedded in a hex body.

        Supports 8-bit reference (IEI=0x00, UDHL=0x05) and 16-bit reference
        (IEI=0x08, UDHL=0x06) formats.

        Returns ``(ref, total_parts, part_num, message_hex)`` when a valid
        concatenation UDH is found, or ``None`` otherwise.
        """
        try:
            if len(hex_body) < 12:
                return None
            udhl = int(hex_body[0:2], 16)
            iei  = int(hex_body[2:4], 16)
            iel  = int(hex_body[4:6], 16)
            skip = (udhl + 1) * 2  # bytes to skip = (UDHL value + 1) × 2 hex chars
            if iei == 0x00 and iel == 0x03 and udhl == 0x05 and len(hex_body) >= skip:
                ref   = int(hex_body[6:8], 16)
                total = int(hex_body[8:10], 16)
                part  = int(hex_body[10:12], 16)
                return ref, total, part, hex_body[skip:]
            if iei == 0x08 and iel == 0x04 and udhl == 0x06 and len(hex_body) >= skip:
                ref   = (int(hex_body[6:8], 16) << 8) | int(hex_body[8:10], 16)
                total = int(hex_body[10:12], 16)
                part  = int(hex_body[12:14], 16)
                return ref, total, part, hex_body[skip:]
        except (ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _decode_ucs2_hex(hex_str: str) -> str:
        """Decode a UCS2 (UTF-16 BE) hex string to a Python unicode string.

        Falls back to the original string if decoding fails.
        """
        try:
            return bytes.fromhex(hex_str).decode('utf-16-be')
        except (ValueError, UnicodeDecodeError):
            return hex_str

    @staticmethod
    def _decode_decimal_ascii_sender(s: str) -> str | None:
        """Decode a sender address encoded as concatenated decimal ASCII codes.

        Some modems return alphanumeric sender addresses as a sequence of
        decimal character codes concatenated without separators.
        E.g. ``'Vodafone'`` becomes ``'8611110097102111110101'``
        (86=V, 111=o, 100=d, 97=a, 102=f, 111=o, 110=n, 101=e).

        Returns the decoded string, or ``None`` if the input cannot be fully
        decoded into printable ASCII characters (code points 32–126).
        """
        result = []
        i = 0
        while i < len(s):
            matched = False
            for length in (3, 2):
                if i + length <= len(s):
                    try:
                        code = int(s[i:i + length])
                    except ValueError:
                        continue
                    if 32 <= code <= 126:
                        result.append(chr(code))
                        i += length
                        matched = True
                        break
            if not matched:
                return None
        return ''.join(result) if result else None

    @staticmethod
    def _parse_sms_list(raw: str) -> list:
        """Parse the AT+CMGL="ALL" response into a list of message dicts.

        Message bodies that are UCS2 hex-encoded are decoded to readable text.
        Multipart (concatenated) SMS parts include ``concat_ref``,
        ``concat_total``, and ``concat_part`` keys so that callers can
        reassemble them.
        """
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
                # Decode sender address when the modem returns it in UCS2 hex
                # or as concatenated decimal ASCII codes (e.g. some modems
                # encode 'Vodafone' as '8611110097102111110101').
                sender_hex = sender.replace(" ", "")
                if ModemManager._is_ucs2_hex(sender_hex):
                    sender = ModemManager._decode_ucs2_hex(sender_hex)
                elif sender.isdigit() and len(sender) > 15:
                    decoded = ModemManager._decode_decimal_ascii_sender(sender)
                    if decoded:
                        sender = decoded
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

                # Decode UCS2 hex-encoded bodies and extract multipart UDH info.
                body_hex = body.replace(" ", "")
                concat_info = None
                if ModemManager._is_ucs2_hex(body_hex):
                    udh = ModemManager._parse_udh_concat(body_hex)
                    if udh:
                        ref, total, part, msg_hex = udh
                        body = ModemManager._decode_ucs2_hex(msg_hex)
                        concat_info = {"ref": ref, "total": total, "part": part}
                    else:
                        body = ModemManager._decode_ucs2_hex(body_hex)

                entry = {
                    "index": idx,
                    "status": status,
                    "sender": sender,
                    "timestamp": ts,
                    "message": body,
                }
                if concat_info:
                    entry["concat_ref"]   = concat_info["ref"]
                    entry["concat_total"] = concat_info["total"]
                    entry["concat_part"]  = concat_info["part"]
                messages.append(entry)
            else:
                i += 1
        return messages
