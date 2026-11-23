# Network Status monitor — Linux container image.
# Best run on a Linux host that sits ON the monitored LAN, with host networking
# so ICMP pings and the radio WebSocket reach 10.2.60.x directly. See README.
FROM python:3.12-slim

# iputils-ping provides the `ping` binary the app shells out to.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py index.html ./

# Device list lives on a mounted volume so it survives container rebuilds.
ENV DEVICES_FILE=/data/devices.json \
    PORT=8766 \
    BIND=0.0.0.0
VOLUME ["/data"]
EXPOSE 8766

# ICMP ping from inside the container needs the NET_RAW capability — the compose
# files add it (cap_add: NET_RAW). See README for plain `docker run`.
CMD ["python", "-u", "monitor.py"]
