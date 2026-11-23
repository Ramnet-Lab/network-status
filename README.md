# Network Status

Live up/down + packet-loss + radio-battery dashboard for the AMP5 LAN. Pings
every device once a second and reads Wave Relay radio battery over the radios'
own read-only management WebSocket. Served at `http://<host>:8766`.

> **The monitor must run on a machine that is ON the monitored LAN.** It reaches
> the devices itself (ICMP ping + radio WebSocket). A browser — local or remote —
> only views the result; it never does the checks. So where you *run* this
> decides whether checks work; where you *view* it does not.

## Files

| File | Purpose |
|------|---------|
| `monitor.py` | the server: pings, battery polling, HTTP + JSON API |
| `index.html` | the dashboard UI |
| `data/devices.json` | the device list (persisted) |
| `requirements.txt` | Python deps (`openpyxl`, `websockets`) |
| `Dockerfile`, `docker-compose*.yml` | container deployment |
| `run-windows.bat` | native Windows launcher |
| `start.command` | native macOS launcher |

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `PORT` | `8766` | HTTP port |
| `BIND` | `0.0.0.0` | bind address |
| `DEVICES_FILE` | `./devices.json` | device-list path |
| `POWER_GATEWAYS` | `10.2.60.215` | comma-separated radio(s) that report battery |
| `RADIO_USER` | `factory` | radio login user |
| `RADIO_PASSWORD` | `password` | radio login password |

---

## A) Linux host on the LAN — Docker (recommended)

Reliable: host networking lets the container ping `10.2.60.x` and reach the
radios exactly like a host process.

```bash
docker compose up -d --build      # uses host networking + NET_RAW
# open http://<this-host-ip>:8766
docker compose logs -f            # watch
docker compose down               # stop
```

`network_mode: host` ignores published ports — the app listens on the host's
`8766` directly. Device list persists in `./data/devices.json`.

## B) Native Windows (recommended for a Windows test box)

Reaches the LAN directly — no container networking in the way.

1. Install **Python 3.10+** (python.org or Microsoft Store; tick *Add to PATH*).
2. Double-click **`run-windows.bat`**.

It builds a local venv, installs deps, opens the browser, and starts the
monitor. Windows Firewall may prompt the first time — allow it.

## C) Windows / Mac Docker Desktop — UI/demo only

> ⚠️ Docker Desktop runs containers in a WSL2/VM behind NAT. The container
> **cannot reliably ping your physical `10.2.60.x` LAN** or reach the radios.
> The page loads and you can add/edit devices, but status may stay red and
> battery blank. Use path **B** for a real Windows test.

```powershell
docker compose -f docker-compose.windows.yml up -d --build
# open http://localhost:8766
```

## D) macOS native

```bash
./start.command        # or: python3 -u monitor.py
```

---

## Notes

- **Battery** comes from one radio (`POWER_GATEWAYS`) that reports supply
  voltage + battery % for every radio it manages. Read-only; nothing is changed
  on the radios. The password is read from `RADIO_PASSWORD` (env) — set it there
  rather than hard-coding for a shared deployment.
- **Internet access with login:** don't expose port 8766 directly. Put
  Cloudflare Tunnel + Access (or a reverse proxy with auth) in front of the
  host. The checks stay on the LAN; only the UI is published.
- **Firewall:** the host must allow inbound `8766` for other machines to view.
