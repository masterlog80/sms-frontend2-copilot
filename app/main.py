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
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

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

MAX_LOG_ENTRIES = 500
# 24 h at 5 s intervals = 17 280 readings
MAX_SIGNAL_HISTORY = 17280
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
settings: dict = {"auto_delete_from_sim": False}

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


# ---------------------------------------------------------------------------
# Modem polling loop
# ---------------------------------------------------------------------------
modem = ModemManager(device=MODEM_DEVICES[0])


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
                state["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return

    signal = modem.get_signal_strength()
    memory = modem.get_memory()
    new_sms = modem.list_sms()
    new_sms = _combine_multipart_sms(new_sms)

    added = _merge_sms(new_sms)
    purged = _purge_multipart_fragments()
    if added:
        _append_log("INFO", f"Received {added} new SMS message(s)")
    if added or purged:
        _save_sms()

    # Auto-delete SMS from SIM memory if the setting is enabled and there are
    # messages on the SIM that have already been stored in the application.
    if settings.get("auto_delete_from_sim") and new_sms:
        deleted_count = 0
        for msg in new_sms:
            idx = msg.get("index")
            if idx is not None:
                if modem.delete_sms(idx):
                    deleted_count += 1
        if deleted_count:
            _append_log("INFO", f"Auto-deleted {deleted_count} SMS from SIM memory")

    with _state_lock:
        state["signal"] = signal
        state["memory"] = memory
        state["modem_connected"] = modem.connected
        state["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    _record_signal(signal, state["last_updated"])

    logger.debug("Poll OK — signal=%s memory=%s sms_on_sim=%d", signal, memory, len(new_sms))


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
    ALLOWED_KEYS = {"auto_delete_from_sim"}
    data = request.get_json(force=True) or {}
    updated = {}
    for key in ALLOWED_KEYS:
        if key in data:
            settings[key] = bool(data[key])
            updated[key] = settings[key]
    _save_settings()
    if updated:
        _append_log("INFO", f"Settings updated: {updated}")
    return jsonify({"success": True, "settings": dict(settings)})


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
