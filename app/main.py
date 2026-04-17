"""
Flask backend for the USB Modem Dashboard.

Exposes a REST API consumed by the single-page frontend and also serves
the static frontend files.

Persistent data (SMS history, event log) is written to /data which should
be mapped to a Docker volume.
"""
import json
import logging
import os
import smtplib
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests as http_requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from modem import ModemManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEVICE = os.environ.get("MODEM_DEVICE", "/dev/ttyUSB0")
# Comma-separated list of devices to try in order.  Defaults to USB0-4 with
# the configured MODEM_DEVICE first so that the primary device is always
# attempted before any alternatives.
_default_devices = [DEVICE] + [
    f"/dev/ttyUSB{i}" for i in range(5) if f"/dev/ttyUSB{i}" != DEVICE
]
_raw_devices: list[str] = [
    d.strip()
    for d in os.environ.get("MODEM_DEVICES", ",".join(_default_devices)).split(",")
    if d.strip()
]
# Always try the primary MODEM_DEVICE first, even if MODEM_DEVICES is
# explicitly set and lists a different order.
MODEM_DEVICES: list[str] = [DEVICE] + [d for d in _raw_devices if d != DEVICE]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))   # seconds
DATA_DIR = os.environ.get("DATA_DIR", "/data")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

SMS_FILE = os.path.join(DATA_DIR, "sms.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
SIGNAL_HISTORY_FILE = os.path.join(DATA_DIR, "signal_history.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
RAW_LOG_FILE = os.path.join(DATA_DIR, "raw_modem_log.json")

MAX_LOG_ENTRIES = 500
# 24 h at 5 s intervals = 17 280 readings
MAX_SIGNAL_HISTORY = 17280
# Maximum number of raw modem log entries kept in memory and on disk.
MAX_RAW_LOG_ENTRIES = 2000
# Minimum shared-prefix length (in characters) required to classify a
# shorter message as a garbled fragment of a longer one.  Candidates
# shorter than 2 × this value are not considered (avoids false positives
# for very short messages).
_MIN_GARBLED_PREFIX_LEN = 20

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# ---------------------------------------------------------------------------
# In-memory state (also persisted to /data)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
state = {
    "signal": {"rssi": 99, "ber": 99, "dbm": None, "percent": 0, "quality": "Unknown"},
    "memory": {"used": 0, "total": 0, "free": 0, "percent_used": 0},
    "modem_info": {},
    "modem_connected": False,
    "last_updated": None,
}
sms_list: list = []
event_log: list = []
signal_history: list = []
raw_modem_log: list = []
# Always-on in-memory rolling buffer for the AT Console tab.  Not persisted to
# disk.  Populated regardless of the ``raw_log_enabled`` setting so the console
# is always live.
at_console_log: list = []
MAX_AT_CONSOLE_ENTRIES = 200
settings: dict = {
    "auto_delete_from_sim": False,
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "email_enabled": False,
    "email_username": "",
    "email_password": "",
    "email_smtp_host": "",
    "email_smtp_port": 587,
    "email_use_tls": True,
    "email_protocol": "starttls",
    "email_subject": "New SMS received",
    "email_from": "",
    "email_to": "",
    "gatewayapi_enabled": False,
    "gatewayapi_token": "",
    "gatewayapi_sender": "",
    "gatewayapi_recipient": "",
    "raw_log_enabled": False,
}

# Delay (in seconds) before an SMS is deleted from SIM memory when
# auto_delete_from_sim is enabled.  A 60-second window gives multipart
# messages time to arrive and be reassembled before any parts are removed.
AUTO_DELETE_DELAY = 60
# Queue of (delete_at_timestamp, sim_index) pairs awaiting SIM deletion.
_pending_sim_deletions: list = []
# SIM indices that have already been queued, so they are not re-scheduled
# on every poll cycle while they are still sitting on the SIM.
_scheduled_sim_indices: set = set()
_pending_sim_deletions_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Data persistence helpers
# ---------------------------------------------------------------------------

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_persisted():
    global sms_list, event_log, signal_history
    _ensure_data_dir()
    try:
        if os.path.exists(SMS_FILE) and os.path.getsize(SMS_FILE) > 0:
            with open(SMS_FILE) as f:
                sms_list = json.load(f)
            logger.info("Loaded %d SMS from disk", len(sms_list))
            if _purge_multipart_fragments():
                _save_sms()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load SMS file: %s", exc)
        sms_list = []

    try:
        if os.path.exists(LOGS_FILE) and os.path.getsize(LOGS_FILE) > 0:
            with open(LOGS_FILE) as f:
                event_log = json.load(f)
            logger.info("Loaded %d log entries from disk", len(event_log))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load logs file: %s", exc)
        event_log = []

    try:
        if os.path.exists(SIGNAL_HISTORY_FILE) and os.path.getsize(SIGNAL_HISTORY_FILE) > 0:
            with open(SIGNAL_HISTORY_FILE) as f:
                signal_history = json.load(f)
            logger.info("Loaded %d signal history entries from disk", len(signal_history))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load signal history file: %s", exc)
        signal_history = []

    _load_raw_log()


def _save_sms():
    _ensure_data_dir()
    try:
        with open(SMS_FILE, "w") as f:
            json.dump(sms_list, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not save SMS: %s", exc)


def _save_logs():
    _ensure_data_dir()
    try:
        with open(LOGS_FILE, "w") as f:
            json.dump(event_log[-MAX_LOG_ENTRIES:], f, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not save logs: %s", exc)


def _save_signal_history():
    _ensure_data_dir()
    try:
        with open(SIGNAL_HISTORY_FILE, "w") as f:
            json.dump(signal_history[-MAX_SIGNAL_HISTORY:], f)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not save signal history: %s", exc)


def _load_raw_log():
    global raw_modem_log
    _ensure_data_dir()
    try:
        if os.path.exists(RAW_LOG_FILE) and os.path.getsize(RAW_LOG_FILE) > 0:
            with open(RAW_LOG_FILE) as f:
                raw_modem_log = json.load(f)
            logger.info("Loaded %d raw log entries from disk", len(raw_modem_log))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load raw log file: %s", exc)
        raw_modem_log = []


def _save_raw_log():
    _ensure_data_dir()
    try:
        with open(RAW_LOG_FILE, "w") as f:
            json.dump(raw_modem_log[-MAX_RAW_LOG_ENTRIES:], f)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not save raw log: %s", exc)


def _load_settings():
    global settings
    _ensure_data_dir()
    try:
        if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) > 0:
            with open(SETTINGS_FILE) as f:
                loaded = json.load(f)
            settings.update(loaded)
            logger.info("Loaded settings from disk")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load settings file: %s", exc)


def _save_settings():
    _ensure_data_dir()
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not save settings: %s", exc)


def _append_log(level: str, message: str):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": level,
        "message": message,
    }
    with _state_lock:
        event_log.append(entry)
        if len(event_log) > MAX_LOG_ENTRIES:
            event_log.pop(0)
    _save_logs()


def _on_raw_modem_cmd(timestamp: str, command: str, response: str, scope: str) -> None:
    """Callback invoked by ModemManager._cmd() for every AT command exchange.

    Always appends an entry to the in-memory AT console log (``at_console_log``)
    so that the AT Console tab is live regardless of the raw-logging setting.

    Also appends to the persistent ``raw_modem_log`` when raw logging is enabled.
    Disk persistence for raw_modem_log happens once per poll cycle in
    ``_do_poll()`` to avoid excessive I/O.
    """
    entry = {
        "type": "at_command",
        "timestamp": timestamp,
        "command": command,
        "response": response,
        "scope": scope,
    }
    # Always populate the AT console rolling buffer (in-memory only).
    at_console_log.append(entry)
    while len(at_console_log) > MAX_AT_CONSOLE_ENTRIES:
        del at_console_log[0]

    # Persistent raw log is gated behind the setting.
    if not settings.get("raw_log_enabled"):
        return
    raw_modem_log.append(entry)
    while len(raw_modem_log) > MAX_RAW_LOG_ENTRIES:
        del raw_modem_log[0]


# ---------------------------------------------------------------------------
# Modem polling loop
# ---------------------------------------------------------------------------
modem = ModemManager(device=MODEM_DEVICES[0])
modem.raw_log_callback = _on_raw_modem_cmd


def _combine_multipart_sms(messages: list) -> list:
    """
    Combine concatenated SMS parts into single messages.

    Parts carry ``concat_ref``, ``concat_total``, and ``concat_part`` keys
    (set by the modem parser when a User Data Header is present).  Parts
    that share the same ``(sender, concat_ref)`` key are joined in ascending
    part-number order.  If only some parts are available they are still
    combined so the user sees as much text as possible.

    Regular (non-multipart) messages are returned unchanged.
    """
    groups: dict = {}
    regular: list = []

    for msg in messages:
        if "concat_ref" in msg:
            key = (msg["sender"], msg["concat_ref"])
            if key not in groups:
                groups[key] = {}
            groups[key][msg["concat_part"]] = msg
        else:
            regular.append(msg)

    combined = list(regular)
    for parts in groups.values():
        sorted_nums = sorted(parts.keys())
        first = parts[sorted_nums[0]]
        message_text = "".join(parts[n]["message"] for n in sorted_nums)
        combined.append({
            "index": first["index"],
            "status": first["status"],
            "sender": first["sender"],
            "timestamp": first["timestamp"],
            "message": message_text,
        })

    return combined


def _is_garbled_fragment(candidate_body: str, full_body: str) -> bool:
    """Return True if *candidate_body* looks like a garbled fragment of *full_body*.

    This handles the case where a previous code version (or text-mode read)
    produced a corrupted concatenation of multipart SMS pieces.  Such a
    message is *not* a sub-string of the correct reassembled text, but it
    shares a significant common prefix with it.

    The check requires:
    - *full_body* is strictly longer than *candidate_body*.
    - The first half of *candidate_body* (at least 20 characters) is also
      the start of *full_body*.
    """
    prefix_len = len(candidate_body) // 2
    if prefix_len < _MIN_GARBLED_PREFIX_LEN:
        return False
    if len(full_body) <= len(candidate_body):
        return False
    return full_body.startswith(candidate_body[:prefix_len])


def _purge_multipart_fragments() -> bool:
    """Remove stale individual multipart SMS parts from *sms_list*.

    After multipart parts are reassembled into a combined message the
    individual parts are sometimes left in the persisted list (e.g. from
    a previous run where the concat UDH was not detected, or from a version
    of the code that did not yet combine multipart messages).

    An entry is treated as a stale fragment when, compared to another entry
    from the same sender and timestamp, its body is either:
    - a non-empty proper sub-string of the other body, **or**
    - a garbled shorter version that shares a significant common prefix with
      the longer body (see ``_is_garbled_fragment``).

    Returns ``True`` if any entries were removed (so the caller can save).
    """
    global sms_list
    to_remove: set[int] = set()
    # Group by (sender, timestamp) to avoid unnecessary cross-group comparisons.
    sms_groups: defaultdict[tuple[str | None, str | None], list[tuple[int, str]]] = defaultdict(list)
    for i, msg in enumerate(sms_list):
        body = msg.get("message") or ""
        if body:
            sms_groups[(msg.get("sender"), msg.get("timestamp"))].append((i, body))
    for group in sms_groups.values():
        if len(group) < 2:
            continue
        for candidate_idx, candidate_body in group:
            if candidate_idx in to_remove:
                continue
            for other_idx, other_body in group:
                if other_idx == candidate_idx or other_idx in to_remove:
                    continue
                if candidate_body != other_body and (
                    candidate_body in other_body
                    or _is_garbled_fragment(candidate_body, other_body)
                ):
                    to_remove.add(candidate_idx)
                    break
    if to_remove:
        sms_list[:] = [m for k, m in enumerate(sms_list) if k not in to_remove]
        logger.info("Purged %d stale multipart SMS fragment(s)", len(to_remove))
        return True
    return False


def _is_stale_part(m: dict, sender: str | None, timestamp: str | None, full_text: str) -> bool:
    """Return True if *m* is a stale individual multipart fragment of *full_text*.

    A message is considered stale when it comes from the same sender, carries
    the same timestamp, and its body is either a non-empty proper sub-string
    of *full_text* or a garbled shorter version that shares a significant
    common prefix with *full_text* (see ``_is_garbled_fragment``).
    """
    if m.get("sender") != sender or m.get("timestamp") != timestamp:
        return False
    body = m.get("message") or ""
    if not body or body == full_text:
        return False
    return body in full_text or _is_garbled_fragment(body, full_text)


def _merge_sms(new_messages: list):
    """
    Merge freshly read SMS into the global list, preserving messages that
    have been read from disk but are no longer on the SIM (already deleted
    there).  New arrivals are prepended so the UI shows them at the top.

    When a newly added message is longer than an existing entry from the same
    sender and timestamp, and the existing entry is a proper sub-string of the
    new message body, that existing entry is a stale individual multipart part
    and is removed before the combined message is inserted.
    """
    global sms_list
    existing_keys = {(m["sender"], m["timestamp"], m["message"]) for m in sms_list}
    added = 0
    for msg in reversed(new_messages):
        key = (msg["sender"], msg["timestamp"], msg["message"])
        if key not in existing_keys:
            # Remove stale individual parts that are proper substrings of this message.
            msg_text = msg.get("message") or ""
            if msg_text:
                msg_sender = msg.get("sender")
                msg_ts = msg.get("timestamp")
                kept, removed_keys = [], set()
                for m in sms_list:
                    if _is_stale_part(m, msg_sender, msg_ts, msg_text):
                        removed_keys.add((m["sender"], m["timestamp"], m["message"]))
                    else:
                        kept.append(m)
                if removed_keys:
                    sms_list[:] = kept
                    existing_keys -= removed_keys
            sms_list.insert(0, msg)
            existing_keys.add(key)
            added += 1
    return added


def _schedule_sim_deletions(messages: list) -> None:
    """Queue SIM indices from *messages* for deletion after AUTO_DELETE_DELAY seconds.

    Each unique index is scheduled only once; subsequent polls that still see
    the same message on the SIM will not extend or duplicate the timer.
    """
    delete_at = time.time() + AUTO_DELETE_DELAY
    with _pending_sim_deletions_lock:
        for msg in messages:
            idx = msg.get("index")
            if idx is not None and idx not in _scheduled_sim_indices:
                _pending_sim_deletions.append((delete_at, idx))
                _scheduled_sim_indices.add(idx)


def _process_pending_sim_deletions() -> None:
    """Delete SIM messages whose AUTO_DELETE_DELAY has elapsed."""
    with _pending_sim_deletions_lock:
        if not _pending_sim_deletions:
            return
        now = time.time()
        to_delete: list = []
        remaining: list = []
        for delete_at, idx in _pending_sim_deletions:
            if now >= delete_at:
                to_delete.append(idx)
            else:
                remaining.append((delete_at, idx))
        _pending_sim_deletions[:] = remaining
        for idx in to_delete:
            _scheduled_sim_indices.discard(idx)

    if to_delete:
        deleted_count = 0
        for idx in to_delete:
            if modem.delete_sms(idx):
                deleted_count += 1
        if deleted_count:
            _append_log("INFO", f"Auto-deleted {deleted_count} SMS from SIM memory")


# ---------------------------------------------------------------------------
# Telegram forwarding
# ---------------------------------------------------------------------------

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _forward_to_telegram(msg: dict) -> None:
    """Forward a single SMS message to a Telegram chat.

    Uses the bot token and chat ID stored in *settings*.  Failures are
    logged but never propagated so they cannot disrupt the polling loop.
    """
    token = (settings.get("telegram_bot_token") or "").strip()
    chat_id = (settings.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        logger.warning("Telegram forwarding enabled but bot token or chat ID is not set")
        return

    sender = msg.get("sender") or "Unknown"
    timestamp = msg.get("timestamp") or ""
    body = msg.get("message") or ""
    text = f"\U0001f4f1 New SMS received\nFrom: {sender}\nTime: {timestamp}\n\n{body}"

    try:
        resp = http_requests.post(
            _TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if resp.ok:
            logger.debug("Forwarded SMS from %s to Telegram chat %s", sender, chat_id)
        else:
            data = resp.json() if resp.content else {}
            logger.warning(
                "Telegram API returned %d: %s",
                resp.status_code,
                data.get("description", resp.text),
            )
            _append_log(
                "WARNING",
                f"Telegram forward failed ({resp.status_code}): {data.get('description', '')}",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not forward SMS to Telegram: %s", exc)
        _append_log("WARNING", f"Telegram forward error: {exc}")


# ---------------------------------------------------------------------------
# Email forwarding
# ---------------------------------------------------------------------------

def _send_smtp_email(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    use_tls: bool,
    protocol: str,
    mime_msg,
) -> None:
    """Send *mime_msg* via SMTP.  Raises ``smtplib.SMTPException`` (or other
    network errors) on failure; never silences exceptions so callers can
    handle them appropriately."""
    if use_tls and protocol == "ssl":
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
            if username:
                server.login(username, password)
            server.sendmail(mime_msg["From"], [mime_msg["To"]], mime_msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if use_tls and protocol == "starttls":
                server.starttls()
            if username:
                server.login(username, password)
            server.sendmail(mime_msg["From"], [mime_msg["To"]], mime_msg.as_string())


def _forward_to_email(msg: dict) -> None:
    """Forward a single SMS message by email using SMTP.

    Uses the email settings stored in *settings*.  Failures are logged but
    never propagated so they cannot disrupt the polling loop.
    """
    smtp_host = (settings.get("email_smtp_host") or "").strip()
    smtp_port = int(settings.get("email_smtp_port") or 587)
    username  = (settings.get("email_username")  or "").strip()
    password  = (settings.get("email_password")  or "").strip()
    from_addr = (settings.get("email_from")      or "").strip()
    to_addr   = (settings.get("email_to")        or "").strip()
    subject   = (settings.get("email_subject")   or "New SMS received").strip()
    use_tls   = bool(settings.get("email_use_tls", True))
    protocol  = (settings.get("email_protocol")  or "starttls").strip().lower()

    if not smtp_host or not to_addr:
        logger.warning("Email forwarding enabled but SMTP host or To address is not set")
        return

    sender_num = msg.get("sender") or "Unknown"
    timestamp  = msg.get("timestamp") or ""
    body       = msg.get("message") or ""

    mail_body = f"From: {sender_num}\nTime: {timestamp}\n\n{body}"
    mime_msg = MIMEText(mail_body, "plain", "utf-8")
    mime_msg["Subject"] = subject
    mime_msg["From"]    = from_addr or username
    mime_msg["To"]      = to_addr

    try:
        _send_smtp_email(smtp_host, smtp_port, username, password, use_tls, protocol, mime_msg)
        logger.debug("Forwarded SMS from %s to email %s", sender_num, to_addr)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not forward SMS by email: %s", exc)
        _append_log("WARNING", f"Email forward error: {exc}")


# ---------------------------------------------------------------------------
# GatewayAPI forwarding
# ---------------------------------------------------------------------------

_GATEWAYAPI_URL = "https://gatewayapi.com/rest/mtsms"


def _forward_to_gatewayapi(msg: dict) -> None:
    """Forward a single SMS message via GatewayAPI.

    Uses the API token, sender ID, and recipient phone number stored in
    *settings*.  Failures are logged but never propagated so they cannot
    disrupt the polling loop.
    """
    token     = (settings.get("gatewayapi_token")     or "").strip()
    sender    = (settings.get("gatewayapi_sender")    or "").strip()
    recipient = (settings.get("gatewayapi_recipient") or "").strip()
    if not token or not recipient:
        logger.warning(
            "GatewayAPI forwarding enabled but API token or recipient is not set"
        )
        return

    sender_num = msg.get("sender") or "Unknown"
    timestamp  = msg.get("timestamp") or ""
    body       = msg.get("message") or ""
    text       = f"New SMS received\nFrom: {sender_num}\nTime: {timestamp}\n\n{body}"

    payload: dict = {
        "message":    text,
        "recipients": [{"msisdn": recipient}],
    }
    if sender:
        payload["sender"] = sender

    try:
        resp = http_requests.post(
            _GATEWAYAPI_URL,
            json=payload,
            headers={"Authorization": f"Token {token}"},
            timeout=10,
        )
        if resp.ok:
            logger.debug(
                "Forwarded SMS from %s to %s via GatewayAPI", sender_num, recipient
            )
        else:
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {}
            err_msg = data.get("message") or data.get("error") or resp.text
            logger.warning(
                "GatewayAPI returned %d: %s", resp.status_code, err_msg
            )
            _append_log(
                "WARNING",
                f"GatewayAPI forward failed ({resp.status_code}): {err_msg}",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not forward SMS via GatewayAPI: %s", exc)
        _append_log("WARNING", f"GatewayAPI forward error: {exc}")


def _poll():
    """Background thread: polls the modem every POLL_INTERVAL seconds."""
    logger.info(
        "Polling thread started (interval=%ds, devices=%s)",
        POLL_INTERVAL,
        MODEM_DEVICES,
    )
    _append_log(
        "INFO",
        f"Polling started – will try devices {MODEM_DEVICES} every {POLL_INTERVAL}s",
    )

    while True:
        try:
            _do_poll()
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected polling error: %s", exc)
        time.sleep(POLL_INTERVAL)


def _do_poll():
    global state

    if not modem.connected:
        connected = False
        for device in MODEM_DEVICES:
            modem.disconnect()
            modem.device = device
            connected = modem.connect()
            if connected:
                _append_log("INFO", f"Connected to modem on {device}")
                try:
                    info = modem.get_modem_info()
                    with _state_lock:
                        state["modem_info"] = info
                    _append_log("INFO", f"Modem info: {info}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not fetch modem info: %s", exc)
                break
            else:
                _append_log("WARNING", f"Cannot connect to modem on {device}")

        if not connected:
            with _state_lock:
                state["modem_connected"] = False
                state["signal"] = {"rssi": 99, "ber": 99, "dbm": None, "percent": 0, "quality": "Unknown"}
                state["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return

    signal = modem.get_signal_strength()
    memory = modem.get_memory()
    raw_sms = modem.list_sms()
    new_sms = _combine_multipart_sms(raw_sms)

    added = _merge_sms(new_sms)
    purged = _purge_multipart_fragments()
    if added:
        _append_log("INFO", f"Received {added} new SMS message(s)")
        # Forward newly added messages to Telegram if the feature is enabled.
        if settings.get("telegram_enabled"):
            for msg in sms_list[:added]:
                _forward_to_telegram(msg)
        # Forward newly added messages by email if the feature is enabled.
        if settings.get("email_enabled"):
            for msg in sms_list[:added]:
                _forward_to_email(msg)
        # Forward newly added messages via GatewayAPI if the feature is enabled.
        if settings.get("gatewayapi_enabled"):
            for msg in sms_list[:added]:
                _forward_to_gatewayapi(msg)
    if added or purged:
        _save_sms()

    # Auto-delete SMS from SIM memory if the setting is enabled.
    # Schedule raw (pre-combine) SIM entries for deletion after
    # AUTO_DELETE_DELAY seconds.  This gives multipart messages enough
    # time for all parts to arrive and be reassembled before any part
    # is removed from the SIM.  Already-queued indices are not re-scheduled.
    if settings.get("auto_delete_from_sim"):
        _schedule_sim_deletions(raw_sms)
    _process_pending_sim_deletions()

    with _state_lock:
        state["signal"] = signal
        state["memory"] = memory
        state["modem_connected"] = modem.connected
        state["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _last_updated_ts = state["last_updated"]

    _record_signal(signal, _last_updated_ts)

    # Append a decoded-SMS entry to the raw log when new messages arrived.
    if settings.get("raw_log_enabled") and added:
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        decoded_entry = {
            "type": "sms_decoded",
            "timestamp": ts,
            "count": len(raw_sms),
            "messages": [
                {
                    "index": m.get("index"),
                    "status": m.get("status"),
                    "sender": m.get("sender"),
                    "timestamp": m.get("timestamp"),
                    "message": m.get("message"),
                }
                for m in raw_sms
            ],
        }
        raw_modem_log.append(decoded_entry)
        while len(raw_modem_log) > MAX_RAW_LOG_ENTRIES:
            del raw_modem_log[0]

    if settings.get("raw_log_enabled"):
        _save_raw_log()

    logger.debug("Poll OK — signal=%s memory=%s sms_on_sim=%d", signal, memory, len(raw_sms))


def _record_signal(signal: dict, timestamp: str):
    """Append one reading to the in-memory signal history and persist it."""
    entry = {
        "timestamp": timestamp,
        "percent": signal.get("percent", 0),
        "dbm": signal.get("dbm"),
        "rssi": signal.get("rssi"),
        "quality": signal.get("quality"),
    }
    with _state_lock:
        signal_history.append(entry)
        if len(signal_history) > MAX_SIGNAL_HISTORY:
            signal_history.pop(0)
    _save_signal_history()


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify({
            "signal": state["signal"],
            "memory": state["memory"],
            "modem_info": state["modem_info"],
            "modem_connected": state["modem_connected"],
            "last_updated": state["last_updated"],
            "device": modem.device,
            "devices": MODEM_DEVICES,
            "poll_interval": POLL_INTERVAL,
        })


@app.route("/api/sms")
def api_sms():
    with _state_lock:
        return jsonify({"sms": list(sms_list)})


@app.route("/api/sms/<int:msg_id>", methods=["DELETE"])
def api_delete_sms(msg_id: int):
    global sms_list
    # msg_id here is the list position (0-based) used by the UI.
    # We also attempt to delete from the SIM using the stored modem index.
    with _state_lock:
        if msg_id < 0 or msg_id >= len(sms_list):
            return jsonify({"error": "SMS not found"}), 404
        msg = sms_list[msg_id]
        modem_index = msg.get("index")

    # Try to remove from SIM (may already be gone)
    if modem_index is not None and modem.connected:
        modem.delete_sms(modem_index)

    with _state_lock:
        sms_list.pop(msg_id)

    _save_sms()
    _append_log("INFO", f"Deleted SMS #{msg_id} (modem index {modem_index})")
    return jsonify({"success": True})


@app.route("/api/sms", methods=["DELETE"])
def api_clear_sms():
    global sms_list
    with _state_lock:
        count = len(sms_list)
        msgs_snapshot = list(sms_list)
    # Delete all from SIM
    if modem.connected:
        for msg in msgs_snapshot:
            idx = msg.get("index")
            if idx is not None:
                modem.delete_sms(idx)
    with _state_lock:
        sms_list = []
    _save_sms()
    _append_log("INFO", f"Cleared all {count} SMS messages")
    return jsonify({"success": True, "deleted": count})


@app.route("/api/logs")
def api_logs():
    with _state_lock:
        return jsonify({"logs": list(event_log)})


@app.route("/api/logs", methods=["DELETE"])
def api_clear_logs():
    global event_log
    with _state_lock:
        event_log = []
    _save_logs()
    return jsonify({"success": True})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger an immediate poll."""
    try:
        _do_poll()
        with _state_lock:
            return jsonify({"success": True, "last_updated": state["last_updated"]})
    except Exception:  # noqa: BLE001
        logger.exception("Error during manual refresh")
        return jsonify({"success": False, "error": "Refresh failed; check server logs"}), 500


@app.route("/api/signal_history")
def api_signal_history():
    """Return persisted signal-strength readings.

    Optional query parameter ``since`` (ISO-8601 timestamp): when supplied
    only readings at or after that timestamp are returned, allowing the
    frontend to fetch just the new delta on every refresh cycle.
    """
    since = request.args.get("since")
    with _state_lock:
        if since:
            entries = [e for e in signal_history if e["timestamp"] >= since]
        else:
            entries = list(signal_history)
    return jsonify({"history": entries})


@app.route("/api/networks")
def api_networks():
    """Scan for available networks via AT+COPS=? (may take up to 60 seconds)."""
    if not modem.connected:
        return jsonify({"error": "Modem not connected"}), 503
    networks = modem.scan_networks()
    current = modem.get_current_network()
    _append_log("INFO", f"Network scan complete – found {len(networks)} network(s)")
    return jsonify({"networks": networks, "current": current})


@app.route("/api/networks/select", methods=["POST"])
def api_select_network():
    """Select a network operator.

    Request body (JSON):
      ``{"mode": "auto"}`` – restore automatic selection.
      ``{"mode": "manual", "numeric": "12345"}`` – lock to a specific operator.
    """
    if not modem.connected:
        return jsonify({"error": "Modem not connected"}), 503
    data = request.get_json(force=True) or {}
    mode = data.get("mode", "auto")
    numeric = data.get("numeric")
    if mode == "manual" and not numeric:
        return jsonify({"error": "numeric is required for manual selection"}), 400
    ok = modem.select_network(mode, numeric)
    if ok:
        _append_log("INFO", f"Network selected: mode={mode}" + (f" numeric={numeric}" if numeric else ""))
        return jsonify({"success": True})
    _append_log("WARNING", f"Network selection failed: mode={mode}" + (f" numeric={numeric}" if numeric else ""))
    return jsonify({"success": False, "error": "Selection failed; check modem response"}), 500


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    """Return current application settings."""
    return jsonify({"settings": dict(settings)})


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    """Update application settings.

    Request body (JSON): a partial or full settings object, e.g.
      ``{"auto_delete_from_sim": true}``
    Only recognised keys are accepted; unknown keys are ignored.
    """
    bool_keys = {
        "auto_delete_from_sim", "telegram_enabled", "email_enabled",
        "email_use_tls", "raw_log_enabled", "gatewayapi_enabled",
    }
    str_keys  = {
        "telegram_bot_token", "telegram_chat_id",
        "email_username", "email_password", "email_smtp_host",
        "email_protocol", "email_subject", "email_from", "email_to",
        "gatewayapi_token", "gatewayapi_sender", "gatewayapi_recipient",
    }
    int_keys  = {"email_smtp_port"}
    data = request.get_json(force=True) or {}
    updated = {}
    for key in bool_keys:
        if key in data:
            settings[key] = bool(data[key])
            updated[key] = settings[key]
    for key in str_keys:
        if key in data:
            settings[key] = str(data[key]).strip()
            updated[key] = settings[key]
    for key in int_keys:
        if key in data:
            try:
                settings[key] = int(data[key])
                updated[key] = settings[key]
            except (TypeError, ValueError):
                pass
    _save_settings()
    if updated:
        _append_log("INFO", f"Settings updated: {list(updated.keys())}")
    return jsonify({"success": True, "settings": dict(settings)})


@app.route("/api/settings/test_telegram", methods=["POST"])
def api_test_telegram():
    """Send a test message to the configured Telegram chat.

    Returns ``{"success": true}`` on success, or an error description.
    Uses the token/chat_id from the request body if provided, falling back
    to the persisted settings so the user can test before saving.
    """
    data = request.get_json(force=True) or {}
    token   = str(data.get("telegram_bot_token") or settings.get("telegram_bot_token") or "").strip()
    chat_id = str(data.get("telegram_chat_id")   or settings.get("telegram_chat_id")   or "").strip()

    if not token or not chat_id:
        return jsonify({"success": False, "error": "Bot token and chat ID are required"}), 400

    try:
        resp = http_requests.post(
            _TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": "\u2705 SMS Dashboard: Telegram forwarding test successful!",
            },
            timeout=10,
        )
        if resp.ok:
            return jsonify({"success": True})
        body = resp.json() if resp.content else {}
        return jsonify({"success": False, "error": body.get("description", resp.text)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram test request failed: %s", exc)
        return jsonify({"success": False, "error": "Could not reach Telegram API"}), 500


@app.route("/api/settings/test_email", methods=["POST"])
def api_test_email():
    """Send a test email using the provided (or persisted) email settings.

    Request body fields mirror the email settings keys; any omitted field
    falls back to the persisted value so the user can test before saving.
    Returns ``{"success": true}`` on success, or an error description.
    """
    data = request.get_json(force=True) or {}

    def _val(key, default=""):
        return str(data.get(key) or settings.get(key) or default).strip()

    smtp_host = _val("email_smtp_host")
    to_addr   = _val("email_to")
    from_addr = _val("email_from")
    username  = _val("email_username")
    password  = _val("email_password")
    subject   = _val("email_subject") or "New SMS received"
    protocol  = _val("email_protocol", "starttls").lower()
    use_tls   = bool(data.get("email_use_tls") if "email_use_tls" in data else settings.get("email_use_tls", True))

    try:
        smtp_port = int(data.get("email_smtp_port") or settings.get("email_smtp_port") or 587)
    except (TypeError, ValueError):
        smtp_port = 587

    if not smtp_host or not to_addr:
        return jsonify({"success": False, "error": "SMTP host and To address are required"}), 400

    mime_msg = MIMEText("\u2705 SMS Dashboard: Email forwarding test successful!", "plain", "utf-8")
    mime_msg["Subject"] = f"[Test] {subject}"
    mime_msg["From"]    = from_addr or username
    mime_msg["To"]      = to_addr

    try:
        _send_smtp_email(smtp_host, smtp_port, username, password, use_tls, protocol, mime_msg)
        return jsonify({"success": True})
    except smtplib.SMTPAuthenticationError as exc:
        logger.warning("Email test auth error: %s", exc)
        return jsonify({"success": False, "error": "Authentication failed – check username/password"}), 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("Email test failed: %s", exc)
        return jsonify({"success": False, "error": "Could not send email – check SMTP settings and server logs"}), 500


@app.route("/api/settings/test_gatewayapi", methods=["POST"])
def api_test_gatewayapi():
    """Send a test SMS via GatewayAPI using the provided (or persisted) settings.

    Returns ``{"success": true}`` on success, or an error description.
    """
    data = request.get_json(force=True) or {}

    def _val(key):
        return str(data.get(key) or settings.get(key) or "").strip()

    token     = _val("gatewayapi_token")
    sender    = _val("gatewayapi_sender")
    recipient = _val("gatewayapi_recipient")

    if not token or not recipient:
        return jsonify(
            {"success": False, "error": "API token and recipient phone number are required"}
        ), 400

    payload: dict = {
        "message":    "\u2705 SMS Dashboard: GatewayAPI forwarding test successful!",
        "recipients": [{"msisdn": recipient}],
    }
    if sender:
        payload["sender"] = sender

    try:
        resp = http_requests.post(
            _GATEWAYAPI_URL,
            json=payload,
            headers={"Authorization": f"Token {token}"},
            timeout=10,
        )
        if resp.ok:
            return jsonify({"success": True})
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        err_msg = body.get("message") or body.get("error") or resp.text
        return jsonify({"success": False, "error": err_msg}), 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("GatewayAPI test request failed: %s", exc)
        return jsonify({"success": False, "error": "Could not reach GatewayAPI"}), 500


# ---------------------------------------------------------------------------
# Raw modem log endpoints
# ---------------------------------------------------------------------------

@app.route("/api/raw_log")
def api_raw_log():
    """Return the in-memory raw modem log."""
    with _state_lock:
        return jsonify({"entries": list(raw_modem_log), "count": len(raw_modem_log)})


@app.route("/api/raw_log", methods=["DELETE"])
def api_clear_raw_log():
    """Clear the raw modem log (in-memory and on disk)."""
    global raw_modem_log
    with _state_lock:
        raw_modem_log = []
    _save_raw_log()
    _append_log("INFO", "Raw modem log cleared")
    return jsonify({"success": True})


@app.route("/api/raw_log/export")
def api_export_raw_log():
    """Download the raw modem log as a JSON file."""
    import io
    from flask import Response
    with _state_lock:
        data = list(raw_modem_log)
    content = json.dumps(data, indent=2)
    filename = f"raw_modem_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        io.BytesIO(content.encode("utf-8")),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# AT Console endpoints
# ---------------------------------------------------------------------------

@app.route("/api/at_console")
def api_at_console():
    """Return the in-memory AT console log (always active, not persisted)."""
    return jsonify({"entries": list(at_console_log), "count": len(at_console_log)})


@app.route("/api/at_console", methods=["DELETE"])
def api_clear_at_console():
    """Clear the in-memory AT console log."""
    global at_console_log
    at_console_log = []
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Application startup
# ---------------------------------------------------------------------------

def create_app():
    _load_persisted()
    _load_settings()
    _append_log("INFO", "Dashboard started")
    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return app


if __name__ == "__main__":
    create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)
