#!/usr/bin/env python3
"""AMP5 device status monitor.

Pings every device in devices.json once a second and serves a live status
page at http://localhost:8766 where devices can be added, relabeled, or removed.
On first run, devices.json is seeded from ~/Desktop/Station_Radio_IPs.xlsx.

  green  = steady, no recent packet loss
  yellow = up, but packets dropped in the recent window
  red    = no reply for more than 10 seconds
"""
import asyncio
import ipaddress
import json
import os
import platform
import re
import ssl
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
# devices.json and the seed spreadsheet can be redirected with env vars so a
# container can mount them from a volume. Defaults keep the desktop behavior.
DEVICES_FILE = os.environ.get("DEVICES_FILE", os.path.join(HERE, "devices.json"))
SPREADSHEET = os.environ.get("SPREADSHEET", os.path.expanduser("~/Desktop/Station_Radio_IPs.xlsx"))
PORT = int(os.environ.get("PORT", "8766"))
BIND = os.environ.get("BIND", "0.0.0.0")
PING_INTERVAL = 1.0   # seconds between pings per device
OFFLINE_AFTER = 10.0  # seconds with no reply before red
WINDOW = 30           # recent pings checked for drops (yellow)

# Wave Relay radio power readout. One radio reports supply voltage and battery
# for every radio it manages, via the same read-only "network_get" request its
# own Power Status page uses. Radios in POWER_GATEWAYS are tried first, then
# every device in the "Radios" group, until one answers.
POWER_GATEWAYS = [ip for ip in os.environ.get("POWER_GATEWAYS", "10.2.60.215").split(",") if ip.strip()]
RADIO_USER = os.environ.get("RADIO_USER", "factory")
RADIO_PASSWORD = os.environ.get("RADIO_PASSWORD", "password")
POWER_EVERY = 15      # seconds between power polls
POWER_STALE = 90      # drop a reading older than this

_SYS = platform.system()
if _SYS == "Darwin":
    PING_CMD = ["ping", "-c", "1", "-W", "1000"]  # -W in ms on macOS
elif _SYS == "Windows":
    PING_CMD = ["ping", "-n", "1", "-w", "1000"]  # -n count, -w timeout in ms
else:
    PING_CMD = ["ping", "-c", "1", "-W", "1"]     # -W in seconds on Linux

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

lock = threading.Lock()
devices = []   # [{"name", "ip", "group"}] in display order
state = {}     # ip -> ping state


def coord(val, lo, hi):
    """Parse an optional latitude/longitude; blank -> None, out-of-range -> error."""
    if val is None or str(val).strip() == "":
        return None
    f = float(val)  # ValueError bubbles up to the 400 handler
    if not lo <= f <= hi:
        raise ValueError("out of range")
    return f


def valid_address(addr):
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        return bool(HOSTNAME_RE.match(addr))


def seed_from_spreadsheet():
    try:
        from openpyxl import load_workbook
        wb = load_workbook(SPREADSHEET, read_only=True)
        rows = [{"name": str(n).strip(), "ip": str(ip).strip(), "group": "Radios"}
                for n, ip in wb.active.iter_rows(min_row=2, max_col=2, values_only=True)
                if n is not None and ip]
        wb.close()
        return rows
    except Exception as e:
        print(f"could not seed from spreadsheet: {e}")
        return []


def load_devices():
    if os.path.exists(DEVICES_FILE):
        with open(DEVICES_FILE) as f:
            return json.load(f)
    rows = seed_from_spreadsheet()
    save_devices(rows)
    return rows


def save_devices(rows):
    tmp = DEVICES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=2)
    os.replace(tmp, DEVICES_FILE)


def ping(ip):
    """Return latency in ms, or None if no reply."""
    try:
        out = subprocess.run(PING_CMD + [ip], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        return None
    # Windows ping exits 0 even for "Destination host unreachable" router
    # replies, so require a real echo reply (TTL=) before trusting it.
    if _SYS == "Windows" and "TTL=" not in out.stdout.upper():
        return None
    if out.returncode != 0:
        return None
    m = re.search(r"time[=<]([\d.]+)", out.stdout)
    return float(m.group(1)) if m else 0.0


def worker(ip):
    """Ping one address until it is removed from the device list."""
    while True:
        with lock:
            st = state.get(ip)
            if st is None:
                return
        latency = ping(ip)
        with lock:
            if state.get(ip) is not st:
                return
            st["history"].append(latency is not None)
            del st["history"][:-WINDOW]
            if latency is not None:
                st["last_ok"] = time.time()
                st["latency"] = latency
        time.sleep(PING_INTERVAL)


def start_tracking(ip):
    # caller holds lock
    if ip not in state:
        state[ip] = {"history": [], "last_ok": None, "latency": None, "started": time.time()}
        threading.Thread(target=worker, args=(ip,), daemon=True).start()


power = {}  # ip -> {"voltage", "display", "battery_pct", "at"}
gps = {}    # ip -> {"lat", "lon", "fix", "raw", "at"}  (from the radio GPS)


def parse_gps(raw):
    """Pull lat/lon out of the radio's gps_status text.

    With no satellite fix the radio sends only "Fix Mode: No Fix" and no
    coordinates. When fixed it adds Latitude/Longitude lines; parse decimal
    degrees from them. (Format confirmed only for the No-Fix case so far —
    see README; verify against a real fix.)
    """
    fix = "no fix" not in raw.lower()
    lat = lon = None
    mlat = re.search(r"Lat(?:itude)?\s*[:=]?\s*(-?\d+\.\d+)", raw, re.I)
    mlon = re.search(r"Lon(?:gitude)?\s*[:=]?\s*(-?\d+\.\d+)", raw, re.I)
    if mlat and mlon:
        lat, lon = float(mlat.group(1)), float(mlon.group(1))
    return {"lat": lat, "lon": lon, "fix": fix, "raw": raw.strip()}


async def fetch_radio(gateway):
    """One WebSocket read of battery + GPS for every node the gateway manages."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # radios use self-signed certs
    msg = {
        "username": RADIO_USER, "password": RADIO_PASSWORD, "token": "status-page",
        "msgtype": "req", "command": "network_get", "protocol_version": "1.5.0",
        "intermediate_responses": True,
        "variables": {"supply_voltage": {}, "supply_battery_percent_display": {}, "gps_status": {}},
    }
    pw, loc = {}, {}
    async with websockets.connect(f"wss://{gateway}/xxx", ssl=ctx, open_timeout=5) as ws:
        await ws.send(json.dumps(msg))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            for ip, node in (reply.get("ip_list") or {}).items():
                v = node.get("variables")
                if not v:
                    continue
                volts = (v.get("supply_voltage") or {}).get("value", "")
                disp = (v.get("supply_battery_percent_display") or {}).get("value", "")
                m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", disp)  # radio sends e.g. "39"
                pw[ip] = {
                    "voltage": volts.replace(" Volts", " V"),
                    "display": disp,
                    "battery_pct": float(m.group(1)) if m else None,
                }
                raw = (v.get("gps_status") or {}).get("value", "")
                if raw:
                    loc[ip] = parse_gps(raw)
            if "final_status" in reply:
                return pw, loc


def power_poller():
    while True:
        with lock:
            radios = [d["ip"] for d in devices if d["group"].lower().startswith("radio")]
        for gw in dict.fromkeys(POWER_GATEWAYS + radios):
            try:
                pw, loc = asyncio.run(fetch_radio(gw))
            except Exception:
                continue
            if pw or loc:
                now = time.time()
                with lock:
                    for ip, rec in pw.items():
                        power[ip] = {**rec, "at": now}
                    for ip, rec in loc.items():
                        gps[ip] = {**rec, "at": now}
                break
        time.sleep(POWER_EVERY)


def power_for(ip, now):
    p = power.get(ip)
    if not p or now - p["at"] > POWER_STALE:
        return None
    return {k: p[k] for k in ("voltage", "display", "battery_pct")}


def location_for(dev, now):
    """Live radio GPS fix wins; otherwise the manually set coordinate."""
    g = gps.get(dev["ip"])
    if g and g["lat"] is not None and now - g["at"] <= POWER_STALE:
        return {"lat": g["lat"], "lon": g["lon"], "source": "gps"}
    if dev.get("lat") is not None and dev.get("lon") is not None:
        return {"lat": dev["lat"], "lon": dev["lon"], "source": "manual"}
    return None


def snapshot():
    now = time.time()
    out = []
    with lock:
        for d in devices:
            st = state[d["ip"]]
            hist = st["history"]
            since_ok = now - (st["last_ok"] or st["started"])
            if not hist:
                status = "pending"
            elif since_ok > OFFLINE_AFTER:
                status = "red"
            elif not all(hist):
                status = "yellow"
            else:
                status = "green"
            out.append({
                **d, "status": status,
                "loss_pct": round(100 * hist.count(False) / len(hist)) if hist else None,
                "latency_ms": st["latency"] if since_ok <= OFFLINE_AFTER else None,
                "last_ok_ago": round(now - st["last_ok"]) if st["last_ok"] else None,
                "power": power_for(d["ip"], now),
                "location": location_for(d, now),
            })
    return out


def upsert_device(name, ip, group, orig_ip=None, lat=None, lon=None):
    """Add a device, or update the one at orig_ip (label, address, group, coords)."""
    key = orig_ip or ip
    with lock:
        if orig_ip and orig_ip != ip and any(d["ip"] == ip for d in devices):
            return f"{ip} is already being tracked"
        for d in devices:
            if d["ip"] == key:
                if d["ip"] != ip:
                    state.pop(d["ip"], None)
                    start_tracking(ip)
                d["name"], d["ip"], d["group"] = name, ip, group
                d["lat"], d["lon"] = lat, lon
                break
        else:
            devices.append({"name": name, "ip": ip, "group": group, "lat": lat, "lon": lon})
            start_tracking(ip)
        save_devices(devices)
    return None


def remove_device(ip):
    with lock:
        devices[:] = [d for d in devices if d["ip"] != ip]
        state.pop(ip, None)
        save_devices(devices)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self.send_json(200, snapshot())
        elif path in ("/", "/index.html"):
            self.path = "/index.html"
            super().do_GET()
        elif path.startswith("/vendor/") and ".." not in path:
            super().do_GET()  # served from HERE by SimpleHTTPRequestHandler
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/devices":
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            name = str(data.get("name", "")).strip()[:60]
            ip = str(data.get("ip", "")).strip()
            group = str(data.get("group", "")).strip()[:40] or "Other"
            orig_ip = str(data.get("orig_ip") or "").strip() or None
            lat = coord(data.get("lat"), -90, 90)
            lon = coord(data.get("lon"), -180, 180)
        except (ValueError, AttributeError):
            return self.send_json(400, {"error": "bad request"})
        if not name:
            return self.send_json(400, {"error": "Name is required"})
        if not valid_address(ip):
            return self.send_json(400, {"error": "Not a valid IP address or hostname"})
        if (lat is None) != (lon is None):
            return self.send_json(400, {"error": "Enter both latitude and longitude, or neither"})
        err = upsert_device(name, ip, group, orig_ip, lat, lon)
        if err:
            return self.send_json(400, {"error": err})
        self.send_json(200, {"ok": True})

    def do_DELETE(self):
        url = urlparse(self.path)
        if url.path != "/api/devices":
            return self.send_error(404)
        ip = parse_qs(url.query).get("ip", [""])[0]
        remove_device(ip)
        self.send_json(200, {"ok": True})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    devices.extend(load_devices())
    with lock:
        for d in devices:
            start_tracking(d["ip"])
    threading.Thread(target=power_poller, daemon=True).start()
    print(f"AMP5 status: http://localhost:{PORT}  (binding {BIND}:{PORT})", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
