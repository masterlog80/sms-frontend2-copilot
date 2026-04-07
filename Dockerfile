# ── Build stage (install Python deps) ──────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /install
COPY app/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/pkg -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="USB Modem Dashboard"
LABEL description="HSDPA USB modem dashboard: signal strength, SMS inbox, memory usage"

# Serial-port utilities
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        udev \
        libusb-1.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install/pkg /usr/local

# Copy application
WORKDIR /app
COPY app/ .

# Create data volume directory
RUN mkdir -p /data && chmod 777 /data

# Expose web UI port
EXPOSE 5000

# Environment defaults (can be overridden in docker-compose.yml)
ENV MODEM_DEVICE=/dev/ttyUSB0 \
    MODEM_DEVICES=/dev/ttyUSB0,/dev/ttyUSB1,/dev/ttyUSB2,/dev/ttyUSB3,/dev/ttyUSB4 \
    POLL_INTERVAL=5 \
    DATA_DIR=/data \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1

# Run with gunicorn for production-grade serving
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "main:create_app()"]
