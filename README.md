# USB Modem Dashboard

A Dockerized web dashboard for USB stick SIM modems and compatible AT-command devices. Connects to the modem over `/dev/ttyUSB0`, polls it every 5 seconds, and presents signal strength, SMS inbox, network info, and event logs in a clean dark/light-mode UI.

---

## Features

- 📶 **Signal strength** – RSSI, dBm, quality badge, 5-bar colour-coded indicator (0 bars red → 5 bars green)
- 📈 **Signal history graph** – interactive Chart.js line chart (Signal % + dBm) with selectable time ranges: **10 min · 1 hour · 6 hours · 24 hours**
- 💾 **SIM SMS memory** – used / free / total slots with fill bar
- 📱 **Modem info** – manufacturer, model, IMEI, network registration status
- 📨 **SMS inbox** – all messages on the SIM, with per-message delete
- 📋 **Event log** – timestamped log of every modem event with level filtering
- 📡 **Network selection** – scan all visible operators and manually lock to any (or restore automatic selection)
- 🌙 **Dark / Light mode** toggle (preference saved in `localStorage`)
- 🔄 **Auto-refresh** every 5 seconds with live countdown
- 💾 **Persistent data** – SMS history, signal log, and event log survive container restarts via Docker volume

---

## Quick Start

### Prerequisites

| Requirement | Notes |
|---|---|
| Docker ≥ 24 | `docker --version` |
| Docker Compose ≥ 2.20 | `docker compose version` |
| USB modem on `/dev/ttyUSB0` | Adjust `MODEM_DEVICE` if yours differs |
| Host user in `dialout` group | `sudo usermod -aG dialout $USER` |

> **Tip – multiple ttyUSB ports:** HSDPA sticks often expose several serial interfaces. If AT commands don't work on `ttyUSB0`, try `ttyUSB1` or `ttyUSB2` via the `MODEM_DEVICE` environment variable.

### Clone & build

```bash
git clone https://github.com/masterlog80/sms-frontend2-copilot.git
cd sms-frontend2-copilot

yes | docker image prune --all
docker build -t modem-dashboard .
docker compose -f docker-compose.yml up -d --remove-orphans
```

### Access the UI

```
http://localhost:5000
```

### Uninstall

```bash
# Stop the container (data is preserved in the volume)
docker compose down

# Stop AND remove the persistent volume
docker compose down -v
```

---

## Screenshots

### Dark Mode
![USB Modem Dashboard – dark mode](screenshots/dashboard_dark.png)

#### Signal Strength History (dark) – 10 min view
![Signal chart – dark, 10 min](screenshots/signal_chart_dark.png)

#### Signal Strength History (dark) – 1 hour view
![Signal chart – dark, 1 hour](screenshots/signal_chart_1h.png)

#### Event Log (dark)
![Event Log – dark](screenshots/event_log.png)

#### Network Selection – Settings tab (dark)
![Settings – dark](screenshots/settings_dark.png)

### Light Mode
![USB Modem Dashboard – light mode](screenshots/dashboard_light.png)

#### Signal Strength History (light)
![Signal chart – light](screenshots/signal_chart_light.png)

#### Event Log (light)
![Event Log – light](screenshots/event_log_light.png)

#### Network Selection – Settings tab (light)
![Settings – light](screenshots/settings_light.png)

---

## Docker Compose

```yaml
version: "3.9"

services:
  modem-dashboard:
    build:
      context: .
      dockerfile: Dockerfile
    image: modem-dashboard:latest
    container_name: modem-dashboard
    restart: unless-stopped
    privileged: true
    devices:
      - /dev/ttyUSB0:/dev/ttyUSB0
      # Uncomment if your modem uses a different port:
      # - /dev/ttyUSB1:/dev/ttyUSB1
      # - /dev/ttyUSB2:/dev/ttyUSB2
    volumes:
      - modem_data:/data
    ports:
      - "5000:5000"
    environment:
      MODEM_DEVICE: /dev/ttyUSB0
      POLL_INTERVAL: 5
      DATA_DIR: /data
      LOG_LEVEL: INFO
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/status')"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  modem_data:
    driver: local
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MODEM_DEVICE` | `/dev/ttyUSB0` | Serial device path |
| `POLL_INTERVAL` | `5` | Seconds between modem polls |
| `FORWARD_DELAY` | `30` | Seconds to wait before forwarding SMS (allows multipart messages to be assembled) |
| `DATA_DIR` | `/data` | Directory for persistent JSON files |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/status` | Signal, memory, modem info, connection state |
| `GET` | `/api/sms` | All persisted SMS messages |
| `DELETE` | `/api/sms/<n>` | Delete SMS at list position `n` |
| `DELETE` | `/api/sms` | Delete all SMS messages |
| `GET` | `/api/logs` | Event log entries |
| `DELETE` | `/api/logs` | Clear event log |
| `GET` | `/api/signal_history` | Signal history (optional `?since=<ISO>`) |
| `POST` | `/api/refresh` | Trigger an immediate modem poll |
| `GET` | `/api/networks` | Scan for available operators (AT+COPS=?, up to 60 s) |
| `POST` | `/api/networks/select` | Select operator: `{"mode":"auto"}` or `{"mode":"manual","numeric":"<MCC+MNC>"}` |

---

## Persistent Data

| File | Contents |
|---|---|
| `/data/sms.json` | All received SMS (merged; never overwritten unless deleted) |
| `/data/logs.json` | Up to 500 most-recent event log entries |
| `/data/signal_history.json` | Up to 17 280 signal readings (~24 h at 5 s intervals) |

Backup or inspect on the host:

```bash
docker cp modem-dashboard:/data ./modem-data-backup
```

---

## Troubleshooting

**Modem not detected**
```bash
ls -la /dev/ttyUSB*
sudo chmod a+rw /dev/ttyUSB0
# Verify AT interface: screen /dev/ttyUSB0 115200  then type: AT
```

**Permission denied on `/dev/ttyUSB0`**
```bash
sudo usermod -aG dialout $USER  # then log out and back in
```

**Dashboard shows "Disconnected"**: Check the Event Log panel, try a different `MODEM_DEVICE` value, and confirm the container is running with `privileged: true`.

---

## Project Structure

```
.
├── docker-compose.yml
├── Dockerfile
├── app/
│   ├── main.py               # Flask backend + polling loop
│   ├── modem.py              # AT-command modem interface (pyserial)
│   ├── requirements.txt
│   └── static/
│       ├── index.html        # Single-page UI (Bootstrap 5)
│       ├── app.js
│       └── style.css
└── README.md
```

---

## License

MIT
