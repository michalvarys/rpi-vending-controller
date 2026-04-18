"""Trafika vending controller — webhook receiver + live dashboard (env-configured)."""
import ctypes
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

from flask import Flask, abort, jsonify, render_template_string, request

WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "").strip()
if not WEBHOOK_TOKEN:
    print("FATAL: WEBHOOK_TOKEN env var is required", file=sys.stderr)
    sys.exit(1)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DEVICE_NAME = os.environ.get("DEVICE_NAME", socket.gethostname())
LOCATION = os.environ.get("LOCATION", "").strip()
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = DATA_DIR / "events.log"

START_TIME = time.time()

app = Flask(__name__)

_state = {"relay": "OFF", "changed_at": None, "changed_by": None}
_state_lock = Lock()

_health = {"internet_ok": False, "internet_checked_at": None, "public_ip": None, "public_ip_checked_at": None}
_health_lock = Lock()


def log_event(event, source="", note=""):
    line = json.dumps({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "source": source,
        "note": note,
    })
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def tail_log(n=100):
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open() as f:
        lines = f.readlines()[-n:]
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def set_relay(target, source):
    # TODO: swap for gpiozero OutputDevice once relay pin is confirmed.
    with _state_lock:
        previous = _state["relay"]
        _state["relay"] = target
        _state["changed_at"] = datetime.now().isoformat(timespec="seconds")
        _state["changed_by"] = source
    log_event(f"relay_{target.lower()}", source=source, note=f"{previous} -> {target}")


def require_token():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.args.get("token", "")
    if token != WEBHOOK_TOKEN:
        abort(401)


def _source_from_request(default):
    if request.is_json and request.json:
        return request.json.get("source", default)
    return default


def _state_payload():
    return {**_state, "device": DEVICE_NAME}


# ----- Device / status helpers -----

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip().rstrip("\x00")
    except OSError:
        return None


def _cpuinfo_field(name):
    data = _read("/proc/cpuinfo") or ""
    for line in data.splitlines():
        if line.startswith(name):
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


def _cpu_temp_c():
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw) / 1000.0, 1) if raw else None
    except ValueError:
        return None


def _memory():
    data = _read("/proc/meminfo") or ""
    total_kb = avail_kb = None
    for line in data.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])
    if total_kb and avail_kb is not None:
        used_pct = round((total_kb - avail_kb) / total_kb * 100, 1)
        return total_kb // 1024, used_pct
    return None, None


def _load_1m():
    data = _read("/proc/loadavg")
    try:
        return float(data.split()[0]) if data else None
    except (ValueError, IndexError):
        return None


def _host_os():
    for candidate in ("/etc/host-os-release", "/etc/os-release"):
        data = _read(candidate)
        if not data:
            continue
        m = re.search(r'^PRETTY_NAME="?([^"\n]+?)"?\s*$', data, re.MULTILINE)
        if m:
            return m.group(1)
    return None


def _host_hostname():
    return _read("/etc/host-hostname") or socket.gethostname()


def _check_internet():
    try:
        s = socket.create_connection(("1.1.1.1", 53), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def _fetch_public_ip():
    try:
        req = urllib.request.Request(
            "https://api.ipify.org?format=text",
            headers={"User-Agent": "trafika-rpi"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            ip = r.read().decode().strip()
        if re.fullmatch(r"[\d.]+|[\da-fA-F:]+", ip):
            return ip
    except Exception:
        pass
    return None


def _internet_poller():
    while True:
        ok = _check_internet()
        with _health_lock:
            _health["internet_ok"] = ok
            _health["internet_checked_at"] = datetime.now().isoformat(timespec="seconds")
        time.sleep(30)


def _public_ip_poller():
    while True:
        ip = _fetch_public_ip()
        with _health_lock:
            if ip:
                _health["public_ip"] = ip
            _health["public_ip_checked_at"] = datetime.now().isoformat(timespec="seconds")
        time.sleep(3600)


# ----- Webhook endpoints -----

@app.post("/webhook/on")
def webhook_on():
    require_token()
    set_relay("ON", source=_source_from_request("webhook"))
    return jsonify(ok=True, state=_state_payload())


@app.post("/webhook/off")
def webhook_off():
    require_token()
    set_relay("OFF", source=_source_from_request("webhook"))
    return jsonify(ok=True, state=_state_payload())


# ----- Read endpoints -----

@app.get("/api/state")
def api_state():
    return jsonify(_state_payload())


@app.get("/api/logs")
def api_logs():
    return jsonify(tail_log(100))


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, device=DEVICE_NAME)


@app.get("/api/status")
def api_status():
    issues = []

    disk = shutil.disk_usage("/")
    disk_free_mb = disk.free // (1024 * 1024)
    disk_total_mb = disk.total // (1024 * 1024)
    disk_free_pct = round(disk.free / disk.total * 100, 1)
    if disk_free_pct < 10:
        issues.append("low-disk")

    cpu_temp = _cpu_temp_c()
    if cpu_temp is not None and cpu_temp > 80:
        issues.append("high-temp")

    mem_total_mb, mem_used_pct = _memory()
    if mem_used_pct is not None and mem_used_pct > 90:
        issues.append("high-memory")

    with _health_lock:
        internet_ok = _health["internet_ok"]
        internet_checked_at = _health["internet_checked_at"]
    if internet_checked_at and not internet_ok:
        issues.append("no-internet")

    return jsonify({
        "ok": not issues,
        "device": DEVICE_NAME,
        "uptime_seconds": int(time.time() - START_TIME),
        "internet": internet_ok,
        "internet_checked_at": internet_checked_at,
        "disk_free_mb": disk_free_mb,
        "disk_total_mb": disk_total_mb,
        "disk_free_pct": disk_free_pct,
        "cpu_temp_c": cpu_temp,
        "memory_used_pct": mem_used_pct,
        "load_avg_1m": _load_1m(),
        "issues": issues,
    })


@app.get("/api/device")
def api_device():
    mem_total_mb, _ = _memory()
    with _health_lock:
        public_ip = _health["public_ip"]
        public_ip_checked_at = _health["public_ip_checked_at"]
    uname = os.uname()
    return jsonify({
        "device": DEVICE_NAME,
        "location": LOCATION or None,
        "hostname": _host_hostname(),
        "model": _cpuinfo_field("Model"),
        "serial": _cpuinfo_field("Serial"),
        "hw_revision": _cpuinfo_field("Revision"),
        "os": _host_os(),
        "kernel": uname.release,
        "arch": uname.machine,
        "memory_total_mb": mem_total_mb,
        "public_ip": public_ip,
        "public_ip_checked_at": public_ip_checked_at,
    })


@app.post("/ui/toggle")
def ui_toggle():
    target = "ON" if _state["relay"] == "OFF" else "OFF"
    set_relay(target, source="dashboard")
    return jsonify(ok=True, state=_state_payload())


_LINUX_REBOOT_MAGIC1 = 0xfee1dead
_LINUX_REBOOT_MAGIC2 = 672274793
_LINUX_REBOOT_CMD_RESTART = 0x01234567


def _host_reboot():
    time.sleep(1)
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.reboot(_LINUX_REBOOT_MAGIC1, _LINUX_REBOOT_MAGIC2, _LINUX_REBOOT_CMD_RESTART, None)
    except OSError:
        pass
    # If the syscall returns, CAP_SYS_BOOT is missing — fall back to container restart
    # so at least _something_ recovers.
    errno = ctypes.get_errno()
    log_event("host_reboot_failed", source="system", note=f"errno={errno}, falling back to container exit")
    os._exit(0)


@app.post("/api/restart")
def api_restart():
    require_token()
    log_event("host_reboot_requested", source=_source_from_request("webhook"))
    Thread(target=_host_reboot, daemon=True).start()
    return jsonify(ok=True, message="host reboot initiated (~30-60 s)")


@app.get("/")
def index():
    return render_template_string(DASHBOARD_HTML, device=DEVICE_NAME)


DASHBOARD_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>{{ device }} — vending controller</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { --on:#22c55e; --off:#ef4444; --bg:#0f172a; --fg:#e2e8f0; --muted:#64748b; --card:#1e293b; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--fg); padding: 2rem; max-width: 960px; margin: 0 auto; }
  h1 { margin: 0 0 1.5rem; font-size: 1.1rem; color: var(--muted); font-weight: 500; letter-spacing: .05em; }
  .card { background: var(--card); border-radius: 1rem; padding: 2rem; margin-bottom: 1.5rem; }
  .state { display: flex; align-items: center; gap: 2rem; flex-wrap: wrap; }
  .dot { width: 5rem; height: 5rem; border-radius: 50%; box-shadow: 0 0 40px currentColor; transition: all .3s; flex-shrink: 0; }
  .dot.on { background: var(--on); color: var(--on); }
  .dot.off { background: var(--off); color: var(--off); opacity: .7; box-shadow: none; }
  .label { font-size: 3rem; font-weight: 700; line-height: 1; }
  .meta { color: var(--muted); font-size: .9rem; margin-top: .5rem; }
  button { background: #334155; color: var(--fg); border: none; padding: .75rem 1.5rem; border-radius: .5rem; font-size: 1rem; cursor: pointer; font-weight: 500; }
  button:hover { background: #475569; }
  .spacer { flex: 1; }
  .logs { font-family: ui-monospace, monospace; font-size: .85rem; max-height: 26rem; overflow-y: auto; }
  .log-row { padding: .5rem 0; border-bottom: 1px solid #334155; display: grid; grid-template-columns: 12rem 9rem 1fr; gap: 1rem; align-items: baseline; }
  .log-row:last-child { border: 0; }
  .ts { color: var(--muted); font-size: .8rem; }
  .ev-on { color: var(--on); font-weight: 600; }
  .ev-off { color: var(--off); font-weight: 600; }
  h2 { font-size: .8rem; color: var(--muted); text-transform: uppercase; letter-spacing: .1em; margin: 0 0 1rem; }
  .empty { color: var(--muted); font-style: italic; }
</style>
</head>
<body>
<h1>{{ device }} — vending controller</h1>

<div class="card">
  <div class="state">
    <div id="dot" class="dot off"></div>
    <div>
      <div class="label" id="label">OFF</div>
      <div class="meta" id="meta">—</div>
    </div>
    <div class="spacer"></div>
    <button onclick="toggle()">Toggle (test)</button>
  </div>
</div>

<div class="card">
  <h2>Událostní log</h2>
  <div class="logs" id="logs"></div>
</div>

<script>
async function refresh() {
  try {
    const [s, l] = await Promise.all([
      fetch('/api/state').then(r => r.json()),
      fetch('/api/logs').then(r => r.json()),
    ]);
    const on = s.relay === 'ON';
    document.getElementById('dot').className = 'dot ' + (on ? 'on' : 'off');
    document.getElementById('label').textContent = s.relay;
    document.getElementById('meta').textContent = s.changed_at
      ? `Změněno ${s.changed_at} — zdroj: ${s.changed_by || '—'}`
      : 'Zatím žádná změna';
    const html = l.map(e => {
      const cls = e.event.endsWith('_on') ? 'ev-on' : e.event.endsWith('_off') ? 'ev-off' : '';
      const note = e.note ? ' — ' + e.note : '';
      return `<div class="log-row"><span class="ts">${e.ts}</span><span class="${cls}">${e.event}</span><span>${e.source}${note}</span></div>`;
    }).join('');
    document.getElementById('logs').innerHTML = html || '<div class="empty">žádné události</div>';
  } catch (err) {
    console.error(err);
  }
}
async function toggle() {
  await fetch('/ui/toggle', { method: 'POST' });
  refresh();
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    log_event("service_start", source="system", note=f"device={DEVICE_NAME} default_state={_state['relay']} location={LOCATION or '-'}")
    Thread(target=_internet_poller, daemon=True).start()
    Thread(target=_public_ip_poller, daemon=True).start()
    app.run(host=HOST, port=PORT)
