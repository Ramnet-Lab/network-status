#!/usr/bin/env python3
"""Send a CoT rectangle ("box") to WinTAK/ATAK over the SA mesh multicast.

Default target is the TAK SA multicast group 239.2.3.1:6969. WinTAK must be in
mesh/SA mode on the same L2 subnet. On a multi-homed host, pass --iface with the
source IP of the NIC that faces the mesh so the packet egresses the right link.

Example (box south of Homestead ARB, egress the 10.2.60.x NIC):
  python3 send_cot.py --lat 25.46712 --lon -80.39823 --size 500 \
      --callsign "TEST BOX" --iface 10.2.60.201
"""
import argparse
import math
import socket
import struct
import uuid
from datetime import datetime, timedelta, timezone


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def corners(lat, lon, size_m):
    """Four corners of a square of side size_m centered on lat/lon, closed."""
    half = size_m / 2.0
    dlat = half / 111320.0
    dlon = half / (111320.0 * math.cos(math.radians(lat)))
    nw = (lat + dlat, lon - dlon)
    ne = (lat + dlat, lon + dlon)
    se = (lat - dlat, lon + dlon)
    sw = (lat - dlat, lon - dlon)
    return [nw, ne, se, sw, nw]  # closed ring


def build_cot(lat, lon, size_m, callsign, uid, stale_min):
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=stale_min)
    links = "".join(
        f'<link point="{a:.8f},{b:.8f},0"/>' for a, b in corners(lat, lon, size_m)
    )
    # u-d-f = user drawing, free-form polygon. Rendered as a filled box by the
    # closed ring of <link> vertices. Colors are signed 32-bit ARGB.
    return (
        '<?xml version="1.0" standalone="yes"?>'
        f'<event version="2.0" uid="{uid}" type="u-d-f" how="h-e" '
        f'time="{iso(now)}" start="{iso(now)}" stale="{iso(stale)}">'
        f'<point lat="{lat:.8f}" lon="{lon:.8f}" hae="0" ce="9999999" le="9999999"/>'
        '<detail>'
        f'<contact callsign="{callsign}"/>'
        f'{links}'
        '<strokeColor value="-65536"/>'       # opaque red outline
        '<strokeWeight value="4.0"/>'
        '<fillColor value="-1778384896"/>'     # ~40% red fill
        '<labels_on value="true"/>'
        f'<remarks>{callsign} - test box</remarks>'
        '<archive/>'
        '</detail>'
        '</event>'
    )


def build_point(lat, lon, callsign, uid, cot_type, stale_min, course=None, speed=None, hae=0.0):
    """A point marker. With course/speed it carries a <track> so WinTAK draws
    a moving track (heading vector + breadcrumb trail). hae sets altitude."""
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=stale_min)
    how = "m-g" if course is not None else "h-e"  # m-g = GPS-derived, as a real track
    track = f'<track course="{course:.1f}" speed="{speed:.2f}"/>' if course is not None else ""
    return (
        '<?xml version="1.0" standalone="yes"?>'
        f'<event version="2.0" uid="{uid}" type="{cot_type}" how="{how}" '
        f'time="{iso(now)}" start="{iso(now)}" stale="{iso(stale)}">'
        f'<point lat="{lat:.8f}" lon="{lon:.8f}" hae="{hae:.1f}" ce="9999999" le="9999999"/>'
        '<detail>'
        f'<contact callsign="{callsign}"/>'
        f'<remarks>{callsign}</remarks>'
        f'{track}'
        '<archive/>'
        '</detail>'
        '</event>'
    )


def advance(lat, lon, course_deg, dist_m):
    """New lat/lon after moving dist_m along course_deg (0=N, 90=E)."""
    br = math.radians(course_deg)
    dlat = dist_m * math.cos(br) / 111320.0
    dlon = dist_m * math.sin(br) / (111320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def build_delete(uid):
    """CoT that tells receivers to delete the item with the given uid."""
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=1)
    return (
        '<?xml version="1.0" standalone="yes"?>'
        f'<event version="2.0" uid="{uid}-del" type="t-x-d-d" how="m-g" '
        f'time="{iso(now)}" start="{iso(now)}" stale="{iso(stale)}">'
        '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
        '<detail>'
        f'<link uid="{uid}" type="u-d-f" relation="none"/>'
        '<__forcedelete/>'
        '</detail>'
        '</event>'
    )


def _sender(a):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", a.ttl))
    if a.iface:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(a.iface))
        # Bind the source to the mesh NIC so the frame physically egresses THAT
        # interface, not the default-route NIC (multi-homed macOS quirk).
        s.bind((a.iface, 0))
    return s


def _encode(a, xml):
    if a.proto:
        from takproto import xml2proto, TAKProtoVer
        return xml2proto(xml, TAKProtoVer.MESH)
    return xml.encode()


class Transport:
    """Unified send: multicast UDP by default, or persistent TCP to a TAK client
    (ip:port, e.g. WinTAK's 4242) when --tcp is given. TCP sidesteps mesh
    multicast egress entirely and is the reliable path to a known endpoint."""
    def __init__(self, a):
        import time
        self.a, self.time = a, time
        if a.tcp:
            host, port = a.tcp.rsplit(":", 1)
            self.host, self.port = host, int(port)
            self._connect()
        else:
            self.sock = _sender(a)

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((self.host, self.port))

    def send(self, xml):
        a = self.a
        if a.tcp:
            from takproto import xml2proto, TAKProtoVer
            data = xml2proto(xml, TAKProtoVer.STREAM)  # TCP stream framing
            try:
                self.sock.sendall(data)
            except OSError:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.time.sleep(1)
                self._connect()
                self.sock.sendall(data)
        else:
            data = _encode(a, xml)
            for _ in range(a.count):
                try:
                    self.sock.sendto(data, (a.group, a.port))
                except OSError:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.time.sleep(1)
                    self.sock = _sender(a)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def run_track(a):
    """Stream a moving ground track: same UID, advancing position + course/speed,
    so WinTAK renders a track with heading and breadcrumbs."""
    import time
    if a.lat is None or a.lon is None:
        a.error = True
        raise SystemExit("--lat and --lon are required for --track")
    cot_type = a.type if a.type != "a-u-G" else "a-f-G"  # default a moving ground track to friendly
    tx = Transport(a)
    lat, lon = a.lat, a.lon
    steps = max(1, int(a.duration / a.interval))
    for i in range(steps):
        xml = build_point(lat, lon, a.callsign, a.uid, cot_type, a.stale_min,
                          course=a.course, speed=a.speed)
        tx.send(xml)
        time.sleep(a.interval)
        lat, lon = advance(lat, lon, a.course, a.speed * a.interval)
    tx.close()
    print(f"Track done: {steps} updates, uid={a.uid}, "
          f"course {a.course:.0f}deg @ {a.speed:.0f} m/s over {a.duration:.0f}s")


def run_fig8(a):
    """Fly an air track in a figure-8 (Gerono lemniscate) around a center."""
    import time
    if a.lat is None or a.lon is None:
        raise SystemExit("--lat and --lon (center) are required for --fig8")
    lat0, lon0 = a.lat, a.lon
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    tx = Transport(a)
    t0 = time.time()
    end = t0 + a.duration
    prev = None
    while time.time() < end:
        th = 2 * math.pi * ((time.time() - t0) / a.period)
        x = a.radius * math.sin(th)              # east offset (m)
        y = a.radius * math.sin(th) * math.cos(th)  # north offset (m) -> figure 8
        lat = lat0 + y / mlat
        lon = lon0 + x / mlon
        if prev:
            dn = (lat - prev[0]) * mlat
            de = (lon - prev[1]) * mlon
            course = math.degrees(math.atan2(de, dn)) % 360
            speed = math.hypot(dn, de) / a.interval
        else:
            course, speed = 90.0, 0.0
        xml = build_point(lat, lon, a.callsign, a.uid, a.type, a.stale_min,
                          course=course, speed=speed, hae=a.alt)
        tx.send(xml)
        prev = (lat, lon)
        time.sleep(a.interval)
    tx.close()
    print(f"Fig-8 done: uid={a.uid}, type={a.type}, r={a.radius}m alt={a.alt}m period={a.period}s")


def main():
    p = argparse.ArgumentParser(description="Send a CoT box to WinTAK/ATAK.")
    p.add_argument("--lat", type=float)  # required unless --delete
    p.add_argument("--lon", type=float)
    p.add_argument("--size", type=float, default=500.0, help="square side in meters")
    p.add_argument("--callsign", default="TEST BOX")
    p.add_argument("--uid", default="AMP5-TESTBOX-1", help="stable UID; reuse to move the same box")
    p.add_argument("--group", default="239.2.3.1", help="multicast group")
    p.add_argument("--port", type=int, default=6969)
    p.add_argument("--iface", default=None, help="source IP of the NIC facing the mesh")
    p.add_argument("--ttl", type=int, default=32)  # >1 so radios forward across mesh hops
    p.add_argument("--count", type=int, default=3, help="UDP is lossy; send a few")
    p.add_argument("--stale-min", type=int, default=60)
    p.add_argument("--proto", action="store_true",
                   help="send as TAK Protocol v2 mesh protobuf (needs: pip install takproto)")
    p.add_argument("--tcp", default=None, metavar="IP:PORT",
                   help="send over TCP to a TAK client (e.g. 10.2.60.100:4242) instead of multicast")
    p.add_argument("--dry-run", action="store_true", help="print the CoT, send nothing")
    p.add_argument("--delete", action="store_true", help="delete the item with --uid instead of drawing")
    p.add_argument("--marker", action="store_true", help="drop a point marker (object) instead of a box")
    p.add_argument("--type", default="a-u-G", help="CoT type (default a-u-G; a-f-G friendly ground)")
    p.add_argument("--track", action="store_true", help="moving ground track: stream updates with course/speed")
    p.add_argument("--course", type=float, default=270.0, help="track heading deg (0=N,90=E)")
    p.add_argument("--speed", type=float, default=15.0, help="track speed m/s")
    p.add_argument("--interval", type=float, default=1.0, help="track update interval sec")
    p.add_argument("--duration", type=float, default=120.0, help="track run time sec")
    p.add_argument("--fig8", action="store_true", help="fly an air track in a figure-8")
    p.add_argument("--radius", type=float, default=800.0, help="fig-8 half-size meters")
    p.add_argument("--alt", type=float, default=300.0, help="air track altitude (HAE meters)")
    p.add_argument("--period", type=float, default=60.0, help="seconds per full figure-8")
    p.add_argument("--drone", action="store_true", help="shortcut: type a-f-A-M-F-Q (UAV)")
    p.add_argument("--plane", action="store_true", help="shortcut: type a-f-A-M-F (fixed wing)")
    a = p.parse_args()

    if a.drone:
        a.type = "a-f-A-M-F-Q"
    elif a.plane:
        a.type = "a-f-A-M-F"
    if a.fig8:
        if a.type == "a-u-G":
            a.type = "a-f-A-M-F-Q"  # default fig-8 to a drone air track
        return run_fig8(a)
    if a.track:
        return run_track(a)
    if a.delete:
        cot = build_delete(a.uid)
    elif a.marker:
        if a.lat is None or a.lon is None:
            p.error("--lat and --lon are required for --marker")
        cot = build_point(a.lat, a.lon, a.callsign, a.uid, a.type, a.stale_min)
    else:
        if a.lat is None or a.lon is None:
            p.error("--lat and --lon are required unless --delete")
        cot = build_cot(a.lat, a.lon, a.size, a.callsign, a.uid, a.stale_min)
    print(cot)
    payload = cot.encode()
    if a.proto:
        from takproto import xml2proto, TAKProtoVer
        payload = xml2proto(cot, TAKProtoVer.MESH)  # bf 01 bf ... mesh framing
        print(f"(encoded TAK Protocol v2 mesh: {len(payload)} bytes, "
              + " ".join(f"{b:02x}" for b in payload[:3]) + " ...)")
    if a.dry_run:
        return

    tx = Transport(a)
    for _ in range(1 if a.tcp else a.count):
        tx.send(cot)
    tx.close()
    dest = a.tcp if a.tcp else f"{a.group}:{a.port}"
    print(f"\nSent to {dest}" + (f" via {a.iface}" if a.iface and not a.tcp else "") + f"  uid={a.uid}")


if __name__ == "__main__":
    main()
