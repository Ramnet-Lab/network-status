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
import socket
import ssl
import struct
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import websockets

try:
    import takproto  # decode TAK Protocol v2 (protobuf) mesh CoT
except ImportError:
    takproto = None

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

# Discovair sensor array API (JSON over WebSocket at ws://<ip>:PORT/api/v2).
# Read-only status: software version, detection mode, mic-array health.
SENSOR_API_PORT = int(os.environ.get("SENSOR_API_PORT", "8888"))
SENSOR_EVERY = 30     # seconds between sensor polls (these change rarely)
SENSOR_STALE = 150
# Sensor GPS is NOT streamed continuously (saves bandwidth). On demand we open
# the GeoPosition stream briefly, average GPS_SAMPLES fixes, lock the position,
# and persist it. Re-sampled only on startup and when the user clicks Refresh.
GPS_SAMPLES = 10
SENSOR_GPS_FILE = os.environ.get("SENSOR_GPS_FILE", os.path.join(HERE, "sensor_gps.json"))

# CoT ingest: listen to the TAK SA multicast and plot live tracks on the map.
COT_LISTEN = os.environ.get("COT_LISTEN", "1") != "0"
COT_GROUP = os.environ.get("COT_GROUP", "239.2.3.1")
COT_PORT = int(os.environ.get("COT_PORT", "6969"))
COT_IFACE = os.environ.get("COT_IFACE", "")  # source IP of the mesh NIC; "" = default
COT_STALE_SEC = int(os.environ.get("COT_STALE_SEC", "60"))  # drop a track unseen this long

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
    """Live radio GPS fix, then a locked sensor GPS, then a manual coordinate."""
    g = gps.get(dev["ip"])
    if g and g["lat"] is not None and now - g["at"] <= POWER_STALE:
        return {"lat": g["lat"], "lon": g["lon"], "source": "gps"}
    sg = sensor_gps.get(dev["ip"])
    if sg:
        return {"lat": sg["lat"], "lon": sg["lon"], "source": "sensor"}
    if dev.get("lat") is not None and dev.get("lon") is not None:
        return {"lat": dev["lat"], "lon": dev["lon"], "source": "manual"}
    return None


sensor = {}  # ip -> {"version", "mode", "health", "serial", "part", "at"}


async def _ws_cmd(ws, cmd, **extra):
    await ws.send(json.dumps({"command": cmd, "id": cmd, **extra}))
    for _ in range(20):
        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        if r.get("id") == cmd or r.get("command") == cmd:
            return r
    return None


async def fetch_sensor(ip):
    """Read software version, detection mode, and detector health from one sensor."""
    url = f"ws://{ip}:{SENSOR_API_PORT}/api/v2"
    async with websockets.connect(url, open_timeout=5) as ws:
        rec = {}
        ver = await _ws_cmd(ws, "GetSoftwareVersion")
        if ver and ver.get("ok"):
            rec["version"] = ver["data"].get("version")
        mode = await _ws_cmd(ws, "GetMode")
        if mode and mode.get("ok"):
            rec["mode"] = mode["data"].get("mode")
        dets = await _ws_cmd(ws, "ListDetectors")
        ids = (dets or {}).get("data", {}).get("detectorIdentifiers") or []
        if ids:
            ds = await _ws_cmd(ws, "GetDetectorStatus", detectorIdentifier=ids[0])
            if ds and ds.get("ok"):
                d = ds["data"]
                rec.update(health=d.get("health"), serial=d.get("serial"), part=d.get("partNumber"))
        return rec if rec else None


def sensor_poller():
    async def round_once(ips):
        async def one(ip):
            try:
                return ip, await fetch_sensor(ip)
            except Exception:
                return ip, None
        return await asyncio.gather(*(one(ip) for ip in ips))

    while True:
        with lock:
            ips = [d["ip"] for d in devices if d["group"].lower().startswith("sensor")]
        if ips:
            try:
                results = asyncio.run(round_once(ips))
            except Exception:
                results = []
            now = time.time()
            with lock:
                for ip, rec in results:
                    if rec:
                        sensor[ip] = {**rec, "at": now}
        time.sleep(SENSOR_EVERY)


def sensor_for(ip, now):
    s = sensor.get(ip)
    if not s or now - s["at"] > SENSOR_STALE:
        return None
    return {k: s.get(k) for k in ("version", "mode", "health", "serial", "part")}


# Locked sensor GPS positions (averaged once, then fixed). Persisted to disk.
sensor_gps = {}  # ip -> {"lat", "lon", "n", "at"}


def load_sensor_gps():
    try:
        with open(SENSOR_GPS_FILE) as f:
            sensor_gps.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_sensor_gps():
    tmp = SENSOR_GPS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sensor_gps, f, indent=2)
    os.replace(tmp, SENSOR_GPS_FILE)


async def sample_sensor_gps(ip, n=GPS_SAMPLES):
    """Average n GeoPosition fixes from one sensor, then stop the stream."""
    url = f"ws://{ip}:{SENSOR_API_PORT}/api/v2"
    async with websockets.connect(url, open_timeout=5) as ws:
        async def cmd(c, **ex):
            await ws.send(json.dumps({"command": c, "id": c, **ex}))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if r.get("id") == c:
                    return r
        dets = (await cmd("ListDetectors")).get("data", {}).get("detectorIdentifiers") or []
        if not dets:
            return None
        det = dets[0]
        await cmd("InitGeoPositionStream", detectorIdentifier=det)
        lats, lons = [], []
        try:
            while len(lats) < n:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                d = m.get("data") or {}
                if m.get("stream") == "GeoPosition" and d.get("positionFix") \
                        and isinstance(d.get("latitude"), (int, float)):
                    lats.append(d["latitude"])
                    lons.append(d["longitude"])
        finally:
            try:
                await cmd("StopGeoPositionStream", detectorIdentifier=det)
            except Exception:
                pass
        if not lats:
            return None
        return {"lat": sum(lats) / len(lats), "lon": sum(lons) / len(lons), "n": len(lats)}


def refresh_sensor_gps():
    """Sample every sensor's GPS concurrently and lock the averaged positions."""
    with lock:
        ips = [d["ip"] for d in devices if d["group"].lower().startswith("sensor")]

    async def run():
        async def one(ip):
            try:
                return ip, await sample_sensor_gps(ip)
            except Exception:
                return ip, None
        return await asyncio.gather(*(one(ip) for ip in ips))

    results = asyncio.run(run()) if ips else []
    now = time.time()
    located = 0
    with lock:
        for ip, rec in results:
            if rec:
                sensor_gps[ip] = {**rec, "at": now}
                located += 1
        save_sensor_gps()
    return {"sampled": len(ips), "located": located}


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
                "sensor": sensor_for(d["ip"], now),
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


# ---- CoT ingest -----------------------------------------------------------
cot_tracks = {}  # uid -> {name, type, affil, dim, lat, lon, hae, course, speed, at}

AFFIL = {"f": "friendly", "h": "hostile", "n": "neutral", "u": "unknown",
         "s": "suspect", "j": "joker", "k": "faker", "p": "pending", "a": "assumedfriend"}
DIM = {"A": "air", "G": "ground", "S": "sea", "U": "subsurface", "F": "sof", "P": "space"}


def classify(cot_type):
    """Affiliation + dimension from a 2525-style type like a-f-A-M-F-Q."""
    parts = (cot_type or "").split("-")
    affil = AFFIL.get(parts[1], "unknown") if len(parts) > 1 else "unknown"
    dim = DIM.get(parts[2], "other") if len(parts) > 2 else "other"
    return affil, dim


def _store_track(uid, cot_type, name, lat, lon, hae, course, speed):
    if not uid or lat is None or lon is None:
        return
    if lat == 0 and lon == 0:
        return
    if not (cot_type or "").startswith("a-"):
        return  # only atoms (markers/tracks), not drawings/control messages
    affil, dim = classify(cot_type)
    with lock:
        cot_tracks[uid] = {
            "name": name or uid, "type": cot_type, "affil": affil, "dim": dim,
            "lat": lat, "lon": lon, "hae": hae,
            "course": course, "speed": speed, "at": time.time(),
        }


def _parse_proto(data):
    msg = takproto.parse_proto(data)
    if msg is None:
        return
    ev = msg.cotEvent
    d = ev.detail
    name = d.contact.callsign or None
    course = d.track.course if d.track and d.track.speed else None
    speed = d.track.speed if d.track else None
    _store_track(ev.uid, ev.type, name, ev.lat, ev.lon, ev.hae,
                 course if speed else None, speed or None)


def _parse_xml(text):
    root = ET.fromstring(text)
    if root.tag != "event":
        return
    pt = root.find("point")
    if pt is None:
        return
    detail = root.find("detail")
    name = None
    course = speed = None
    if detail is not None:
        c = detail.find("contact")
        if c is not None:
            name = c.get("callsign")
        tr = detail.find("track")
        if tr is not None:
            course = float(tr.get("course")) if tr.get("course") else None
            speed = float(tr.get("speed")) if tr.get("speed") else None
    _store_track(root.get("uid"), root.get("type"), name,
                 float(pt.get("lat")), float(pt.get("lon")),
                 float(pt.get("hae") or 0), course, speed)


def handle_cot(data):
    try:
        if data[:3] == b"\xbf\x01\xbf":
            if takproto:
                _parse_proto(data)
        elif data.lstrip()[:1] == b"<":
            _parse_xml(data.decode("utf-8", "ignore"))
    except Exception:
        pass  # a malformed datagram must not kill the listener


def cot_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", COT_PORT))
    iface = socket.inet_aton(COT_IFACE) if COT_IFACE else struct.pack("=I", socket.INADDR_ANY)
    mreq = socket.inet_aton(COT_GROUP) + iface
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    print(f"CoT ingest: joined {COT_GROUP}:{COT_PORT}"
          + (f" on {COT_IFACE}" if COT_IFACE else ""), flush=True)
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except OSError:
            continue
        handle_cot(data)


def tracks_snapshot(now):
    out = []
    with lock:
        for uid, t in list(cot_tracks.items()):
            if now - t["at"] > COT_STALE_SEC:
                del cot_tracks[uid]
                continue
            out.append({"uid": uid, **{k: t[k] for k in
                        ("name", "type", "affil", "dim", "lat", "lon", "hae", "course", "speed")}})
    return out


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
        elif path == "/api/tracks":
            self.send_json(200, tracks_snapshot(time.time()))
        elif path in ("/", "/index.html"):
            self.path = "/index.html"
            super().do_GET()
        elif path.startswith("/vendor/") and ".." not in path:
            super().do_GET()  # served from HERE by SimpleHTTPRequestHandler
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/sample-gps":
            try:
                result = refresh_sensor_gps()
            except Exception as e:
                return self.send_json(500, {"error": str(e)})
            return self.send_json(200, result)
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
    threading.Thread(target=sensor_poller, daemon=True).start()
    if COT_LISTEN:
        threading.Thread(target=cot_listener, daemon=True).start()
    load_sensor_gps()
    # One-shot sensor GPS sample at startup so the map isn't empty; after that it
    # only refreshes when the user clicks Refresh (no continuous streaming).
    threading.Thread(target=refresh_sensor_gps, daemon=True).start()
    print(f"AMP5 status: http://localhost:{PORT}  (binding {BIND}:{PORT})", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
