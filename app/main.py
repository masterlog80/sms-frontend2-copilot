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
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from modem import ModemManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEVICE = os.environ.get("MODEM_DEVICE", "/dev/ttyUSB0")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))   # seconds
DATA_DIR = os.environ.get("DATA_DIR", "/data")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

SMS_FILE = os.path.join(DATA_DIR, "sms.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")

MAX_LOG_ENTRIES = 500

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

# ---------------------------------------------------------------------------
# Data persistence helpers
# ---------------------------------------------------------------------------

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_persisted():
    global sms_list, event_log
    _ensure_data_dir()
    try:
        if os.path.exists(SMS_FILE) and os.path.getsize(SMS_FILE) > 0:
            with open(SMS_FILE) as f:
                sms_list = json.load(f)
            logger.info("Loaded %d SMS from disk", len(sms_list))
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
modem = ModemManager(device=DEVICE)


def _merge_sms(new_messages: list):
    """
    Merge freshly read SMS into the global list, preserving messages that
    have been read from disk but are no longer on the SIM (already deleted
    there).  New arrivals are prepended so the UI shows them at the top.
    """
    global sms_list
    existing_keys = {(m["sender"], m["timestamp"], m["message"]) for m in sms_list}
    added = 0
    for msg in reversed(new_messages):
        key = (msg["sender"], msg["timestamp"], msg["message"])
        if key not in existing_keys:
            sms_list.insert(0, msg)
            existing_keys.add(key)
            added += 1
    return added


def _poll():
    """Background thread: polls the modem every POLL_INTERVAL seconds."""
    logger.info("Polling thread started (interval=%ds, device=%s)", POLL_INTERVAL, DEVICE)
    _append_log("INFO", f"Polling started on {DEVICE} every {POLL_INTERVAL}s")

    while True:
        try:
            _do_poll()
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected polling error: %s", exc)
        time.sleep(POLL_INTERVAL)


def _do_poll():
    global state

    if not modem.connected:
        connected = modem.connect()
        if connected:
            _append_log("INFO", f"Connected to modem on {DEVICE}")
            try:
                info = modem.get_modem_info()
                with _state_lock:
                    state["modem_info"] = info
                _append_log("INFO", f"Modem info: {info}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not fetch modem info: %s", exc)
        else:
            _append_log("WARNING", f"Cannot connect to modem on {DEVICE}")
            with _state_lock:
                state["modem_connected"] = False
                state["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return

    signal = modem.get_signal_strength()
    memory = modem.get_memory()
    new_sms = modem.list_sms()

    added = _merge_sms(new_sms)
    if added:
        _append_log("INFO", f"Received {added} new SMS message(s)")
        _save_sms()

    with _state_lock:
        state["signal"] = signal
        state["memory"] = memory
        state["modem_connected"] = modem.connected
        state["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    logger.debug("Poll OK — signal=%s memory=%s sms_on_sim=%d", signal, memory, len(new_sms))


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
            "device": DEVICE,
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


# ---------------------------------------------------------------------------
# Application startup
# ---------------------------------------------------------------------------

def create_app():
    _load_persisted()
    _append_log("INFO", "Dashboard started")
    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return app


if __name__ == "__main__":
    create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)
