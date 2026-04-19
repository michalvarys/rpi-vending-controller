"""Trafika central hub — aggregates state across all RPi controllers."""
import base64
import hashlib
import hmac
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

import requests
import yaml
from flask import Flask, Response, abort, jsonify, render_template_string, request

RPIS_FILE = Path(os.environ.get("RPIS_FILE", "/config/rpis.yml"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT", "4"))
# Must match QR_ROTATE_SECONDS on the RPi side. Hub recomputes the same HMAC window.
QR_ROTATE_SECONDS = int(os.environ.get("QR_ROTATE_SECONDS", "60"))

# HTTP Basic Auth gating the dashboard + control endpoints. If empty, hub runs
# wide open (only acceptable on a tailnet-only deployment).
HUB_ADMIN_USER = os.environ.get("HUB_ADMIN_USER", "admin").strip()
HUB_ADMIN_PASSWORD = os.environ.get("HUB_ADMIN_PASSWORD", "").strip()
# Service-to-service token (Authorization: Bearer ...) — used by shop-mock / Odoo
# to call control endpoints without sharing the admin password. If empty, only
# Basic Auth is accepted on protected routes.
HUB_API_TOKEN = os.environ.get("HUB_API_TOKEN", "").strip()
# Paths that bypass auth entirely: server-to-server validate + healthcheck.
_PUBLIC_PATHS = {"/api/qr/validate", "/api/health"}
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hub")


def load_rpis():
    if not RPIS_FILE.exists():
        log.error("RPIS_FILE not found: %s", RPIS_FILE)
        return []
    data = yaml.safe_load(RPIS_FILE.read_text()) or {}
    cleaned = []
    for r in data.get("rpis", []) or []:
        if not r.get("hostname") or not r.get("token"):
            log.warning("Skipping malformed RPi entry (missing hostname/token): %s", r)
            continue
        cleaned.append({
            "hostname": r["hostname"],
            "token": r["token"],
            "display_name": r.get("display_name", r["hostname"]),
            "port": int(r.get("port", 8080)),
        })
    return cleaned


RPIS = load_rpis()
_cache = {}
_cache_lock = Lock()


def rpi_url(rpi, path):
    return f"http://{rpi['hostname']}:{rpi['port']}{path}"


def _get_json(rpi, path):
    try:
        r = requests.get(rpi_url(rpi, path), timeout=POLL_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def poll_one(rpi):
    host = rpi["hostname"]
    now_iso = datetime.now().isoformat(timespec="seconds")

    state = _get_json(rpi, "/api/state")
    if state is None:
        with _cache_lock:
            prev = _cache.get(host, {})
            _cache[host] = {
                "reachable": False,
                "state": prev.get("state"),
                "logs": prev.get("logs", []),
                "status": None,
                "device": prev.get("device"),
                "last_check": now_iso,
                "error": "state endpoint unreachable",
            }
        return

    logs = _get_json(rpi, "/api/logs") or []
    status = _get_json(rpi, "/api/status")
    device = _get_json(rpi, "/api/device")

    with _cache_lock:
        prev = _cache.get(host, {})
        _cache[host] = {
            "reachable": True,
            "state": state,
            "logs": logs[:20],
            "status": status,
            "device": device or prev.get("device"),
            "last_check": now_iso,
            "error": None,
        }


def poller_loop():
    with ThreadPoolExecutor(max_workers=max(10, len(RPIS))) as ex:
        while True:
            if RPIS:
                list(ex.map(poll_one, RPIS))
            time.sleep(POLL_INTERVAL)


def find_rpi(hostname):
    for r in RPIS:
        if r["hostname"] == hostname:
            return r
    abort(404)


app = Flask(__name__)


@app.before_request
def _basic_auth():
    if not HUB_ADMIN_PASSWORD and not HUB_API_TOKEN:
        return None
    if request.path in _PUBLIC_PATHS:
        return None
    # Bearer token first (services), then Basic Auth (humans in browser).
    if HUB_API_TOKEN:
        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].strip(), HUB_API_TOKEN):
            return None
    if HUB_ADMIN_PASSWORD:
        auth = request.authorization
        if auth and auth.username == HUB_ADMIN_USER and auth.password == HUB_ADMIN_PASSWORD:
            return None
    return Response(
        "Auth required",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Trafika Hub"'},
    )


@app.get("/api/dashboard")
def api_dashboard():
    with _cache_lock:
        result = []
        for r in RPIS:
            c = _cache.get(r["hostname"], {
                "reachable": False, "state": None, "logs": [], "status": None,
                "device": None, "last_check": None, "error": "not polled yet",
            })
            result.append({
                "hostname": r["hostname"],
                "display_name": r["display_name"],
                "port": r["port"],
                **c,
            })
    return jsonify(result)


def _call_rpi(rpi, path, with_token=True):
    try:
        headers = {"Authorization": f"Bearer {rpi['token']}"} if with_token else {}
        r = requests.post(
            rpi_url(rpi, path),
            headers=headers,
            json={"source": "hub"},
            timeout=POLL_TIMEOUT,
        )
        r.raise_for_status()
        poll_one(rpi)
        return jsonify(ok=True, state=r.json().get("state"))
    except requests.RequestException as e:
        return jsonify(ok=False, error=str(e)), 502


@app.post("/api/rpi/<hostname>/on")
def api_on(hostname):
    return _call_rpi(find_rpi(hostname), "/webhook/on")


@app.post("/api/rpi/<hostname>/off")
def api_off(hostname):
    return _call_rpi(find_rpi(hostname), "/webhook/off")


@app.post("/api/rpi/<hostname>/toggle")
def api_toggle(hostname):
    return _call_rpi(find_rpi(hostname), "/ui/toggle", with_token=False)


@app.post("/api/rpi/<hostname>/restart")
def api_restart(hostname):
    rpi = find_rpi(hostname)
    try:
        r = requests.post(
            rpi_url(rpi, "/api/restart"),
            headers={"Authorization": f"Bearer {rpi['token']}"},
            json={"source": "hub"},
            timeout=POLL_TIMEOUT,
        )
        r.raise_for_status()
        return jsonify(ok=True, message=r.json().get("message"))
    except requests.RequestException as e:
        return jsonify(ok=False, error=str(e)), 502


def _qr_token(hostname, secret, rotate_seconds, window_offset=0, now=None):
    if now is None:
        now = time.time()
    window = int(now // rotate_seconds) + window_offset
    msg = f"{hostname}:{window}".encode()
    mac = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:9]).decode().rstrip("=")


# Single-use bookkeeping: each (rpi, token) pair can be consumed at most once.
# Entries age out after a few rotation windows.
_consumed_tokens = {}
_consumed_lock = Lock()


def _consume_token(hostname, token, now):
    key = (hostname, token)
    cutoff = now - QR_ROTATE_SECONDS * 4
    with _consumed_lock:
        # Housekeeping: forget entries older than the rotation horizon.
        stale = [k for k, ts in _consumed_tokens.items() if ts < cutoff]
        for k in stale:
            _consumed_tokens.pop(k, None)
        if key in _consumed_tokens:
            return False
        _consumed_tokens[key] = now
        return True


@app.post("/api/qr/validate")
def api_qr_validate():
    payload = request.get_json(silent=True) or {}
    hostname = (payload.get("rpi_hostname") or "").strip()
    token = (payload.get("token") or "").strip()
    if not hostname or not token:
        return jsonify(valid=False, reason="missing rpi_hostname or token"), 400
    rpi = next((r for r in RPIS if r["hostname"] == hostname), None)
    if rpi is None:
        return jsonify(valid=False, reason="unknown rpi"), 404
    now = time.time()
    # HMAC check first — accept current and previous window for clock / scan-to-login drift.
    for offset in (0, -1):
        expected = _qr_token(hostname, rpi["token"], QR_ROTATE_SECONDS, offset, now)
        if hmac.compare_digest(expected, token):
            if not _consume_token(hostname, token, now):
                return jsonify(valid=False, reason="token already used"), 200
            return jsonify(valid=True, rpi_hostname=hostname, window_offset=offset), 200
    return jsonify(valid=False, reason="token mismatch"), 200


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, count=len(RPIS))


@app.get("/")
def index():
    return render_template_string(HUB_HTML)


HUB_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Trafika — central hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {
    --on:#22c55e; --off:#ef4444; --bg:#0f172a; --fg:#e2e8f0;
    --muted:#64748b; --card:#1e293b; --warn:#f59e0b; --ok:#22c55e;
    --border:#334155;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--fg); padding: 2rem; max-width: 1400px; margin: 0 auto; }
  header { display:flex; align-items:baseline; justify-content: space-between; margin-bottom: 2rem; }
  h1 { margin: 0; font-size: 1.25rem; font-weight: 600; letter-spacing: .02em; }
  .updated { color: var(--muted); font-size: .85rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1.25rem; }
  .card { background: var(--card); border-radius: 1rem; padding: 1.5rem; display: flex; flex-direction: column; gap: .9rem; border: 1px solid transparent; }
  .card.warn { border-color: var(--warn); }
  .card.offline { opacity: .6; border-color: var(--off); }

  .top { display: flex; align-items: flex-start; gap: 1rem; }
  .dot { width: 3rem; height: 3rem; border-radius: 50%; flex-shrink: 0; margin-top: .25rem; }
  .dot.on { background: var(--on); box-shadow: 0 0 24px var(--on); }
  .dot.off { background: var(--off); opacity: .7; }
  .dot.unknown { background: var(--muted); }

  .identity { flex: 1; min-width: 0; }
  .title { font-weight: 600; font-size: 1.05rem; }
  .host { color: var(--muted); font-size: .8rem; font-family: ui-monospace, monospace; overflow: hidden; text-overflow: ellipsis; }
  .location { color: var(--fg); font-size: .85rem; margin-top: .1rem; }

  .state-label { font-weight: 700; text-align: right; }
  .state-label.on { color: var(--on); }
  .state-label.off { color: var(--off); }

  .badge { display: inline-block; padding: .15rem .55rem; border-radius: .4rem; font-size: .72rem; font-weight: 600; letter-spacing: .02em; text-transform: uppercase; }
  .badge.ok { background: rgba(34,197,94,.15); color: var(--ok); }
  .badge.warn { background: rgba(245,158,11,.18); color: var(--warn); }
  .badge.off { background: rgba(239,68,68,.18); color: var(--off); }

  .meta { color: var(--muted); font-size: .85rem; }

  .metrics { display: grid; grid-template-columns: repeat(2, 1fr); gap: .4rem .9rem; font-size: .8rem; }
  .metric { display: flex; justify-content: space-between; gap: .5rem; }
  .metric .k { color: var(--muted); }
  .metric .v { font-variant-numeric: tabular-nums; }
  .metric .v.bad { color: var(--off); font-weight: 600; }
  .metric .v.warn { color: var(--warn); font-weight: 600; }
  .metric .v.ok { color: var(--fg); }

  .issues { display: flex; flex-wrap: wrap; gap: .3rem; }
  .issue-chip { background: rgba(245,158,11,.18); color: var(--warn); padding: .15rem .55rem; border-radius: .4rem; font-size: .72rem; font-weight: 500; }

  .actions { display: flex; gap: .5rem; }
  button { flex: 1; background: #334155; color: var(--fg); border: none; padding: .6rem; border-radius: .5rem; font-size: .9rem; cursor: pointer; font-weight: 500; }
  button:hover:not(:disabled) { background: #475569; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  button.on { background: #166534; }
  button.on:hover:not(:disabled) { background: #15803d; }
  button.off { background: #7f1d1d; }
  button.off:hover:not(:disabled) { background: #991b1b; }
  button.restart { background: #78350f; }
  button.restart:hover:not(:disabled) { background: #92400e; }

  details { font-size: .8rem; }
  details summary { cursor: pointer; color: var(--muted); padding: .3rem 0; user-select: none; }
  details summary:hover { color: var(--fg); }
  .device-info { display: grid; grid-template-columns: 8rem 1fr; gap: .25rem .75rem; margin-top: .4rem; font-size: .8rem; }
  .device-info dt { color: var(--muted); }
  .device-info dd { margin: 0; font-family: ui-monospace, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .log-row { padding: .3rem 0; border-bottom: 1px solid var(--border); display: grid; grid-template-columns: 9rem 7rem 1fr; gap: .5rem; font-family: ui-monospace, monospace; font-size: .75rem; }
  .log-row:last-child { border: 0; }
  .log-row .ts { color: var(--muted); }
  .ev-on { color: var(--on); }
  .ev-off { color: var(--off); }
  .empty { color: var(--muted); font-style: italic; padding: .4rem 0; }
  .error { color: var(--warn); font-size: .75rem; font-family: ui-monospace, monospace; word-break: break-word; }
  a.ext { color: #60a5fa; text-decoration: none; font-size: .75rem; }
  a.ext:hover { text-decoration: underline; }
</style>
</head>
<body>
<header>
  <h1>Trafika — central hub</h1>
  <div class="updated" id="updated">—</div>
</header>
<div id="grid" class="grid"></div>

<script>
const ISSUE_LABELS = {
  'no-internet': 'bez internetu',
  'low-disk': 'málo místa',
  'high-temp': 'přehřátí',
  'high-memory': 'vysoká RAM',
};

function captureUI() {
  const open = new Set();
  document.querySelectorAll('details[open]').forEach(el => {
    if (el.dataset.id) open.add(el.dataset.id);
  });
  return { open, scrollY: window.scrollY };
}

function restoreUI(ui) {
  ui.open.forEach(id => {
    const el = document.querySelector(`details[data-id="${CSS.escape(id)}"]`);
    if (el) el.open = true;
  });
  window.scrollTo(0, ui.scrollY);
}

async function refresh() {
  const ui = captureUI();
  let data;
  try {
    data = await fetch('/api/dashboard').then(r => r.json());
  } catch (e) {
    document.getElementById('grid').innerHTML = '<div class="error">Hub API unreachable</div>';
    return;
  }
  document.getElementById('updated').textContent = 'Aktualizováno ' + new Date().toLocaleTimeString('cs-CZ');
  if (data.length === 0) {
    document.getElementById('grid').innerHTML = '<div class="empty">Žádné RPi v rpis.yml. Přidej je a restartuj hub.</div>';
    return;
  }
  document.getElementById('grid').innerHTML = data.map(renderCard).join('');
  restoreUI(ui);
}

function fmtUptime(seconds) {
  if (seconds == null) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function healthBadge(d) {
  if (!d.reachable) {
    return '<span class="badge off">● offline</span>';
  }
  const issues = (d.status && d.status.issues) || [];
  if (issues.length === 0) {
    return '<span class="badge ok">✓ healthy</span>';
  }
  return `<span class="badge warn">⚠ ${issues.length} issue${issues.length > 1 ? 's' : ''}</span>`;
}

function renderMetric(label, value, cls) {
  return `<div class="metric"><span class="k">${label}</span><span class="v ${cls || ''}">${value}</span></div>`;
}

function renderCard(d) {
  const relayState = d.state && d.state.relay;
  const reachable = d.reachable;
  const onRelay = relayState === 'ON';
  const dotClass = !reachable ? 'unknown' : (onRelay ? 'on' : 'off');
  const labelClass = !reachable ? '' : (onRelay ? 'on' : 'off');
  const label = reachable ? relayState : '—';
  const changed = d.state && d.state.changed_at
    ? `Změněno ${d.state.changed_at}${d.state.changed_by ? ' · ' + d.state.changed_by : ''}`
    : 'Zatím žádná změna';

  const status = d.status || {};
  const device = d.device || {};
  const issues = status.issues || [];
  const cardClass = !reachable ? 'offline' : (issues.length ? 'warn' : '');

  const internetOk = status.internet;
  const internetCell = reachable
    ? (internetOk ? renderMetric('Internet', '✓', 'ok') : renderMetric('Internet', '✗', 'bad'))
    : renderMetric('Internet', '—');
  const diskPct = status.disk_free_pct;
  const diskCell = diskPct != null
    ? renderMetric('Volno disku', `${diskPct.toFixed(1)}% (${(status.disk_free_mb/1024).toFixed(1)} GB)`, diskPct < 10 ? 'bad' : (diskPct < 20 ? 'warn' : 'ok'))
    : renderMetric('Volno disku', '—');
  const temp = status.cpu_temp_c;
  const tempCell = temp != null
    ? renderMetric('CPU teplota', `${temp.toFixed(1)}°C`, temp > 80 ? 'bad' : (temp > 70 ? 'warn' : 'ok'))
    : renderMetric('CPU teplota', '—');
  const upCell = renderMetric('Uptime', fmtUptime(status.uptime_seconds));
  const memCell = status.memory_used_pct != null
    ? renderMetric('RAM', `${status.memory_used_pct.toFixed(1)}% použito`, status.memory_used_pct > 90 ? 'bad' : 'ok')
    : renderMetric('RAM', '—');
  const loadCell = status.load_avg_1m != null
    ? renderMetric('Load (1m)', status.load_avg_1m.toFixed(2))
    : renderMetric('Load (1m)', '—');

  const issueChips = issues.length
    ? `<div class="issues">${issues.map(i => `<span class="issue-chip">${ISSUE_LABELS[i] || i}</span>`).join('')}</div>`
    : '';

  const location = device.location ? `<div class="location">📍 ${escapeHtml(device.location)}</div>` : '';

  const deviceInfo = device && (device.model || device.serial)
    ? `<dl class="device-info">
         ${device.model ? `<dt>Model</dt><dd>${escapeHtml(device.model)}</dd>` : ''}
         ${device.serial ? `<dt>Sériové č.</dt><dd>${escapeHtml(device.serial)}</dd>` : ''}
         ${device.hw_revision ? `<dt>HW rev.</dt><dd>${escapeHtml(device.hw_revision)}</dd>` : ''}
         ${device.os ? `<dt>OS</dt><dd>${escapeHtml(device.os)}</dd>` : ''}
         ${device.kernel ? `<dt>Kernel</dt><dd>${escapeHtml(device.kernel)} (${escapeHtml(device.arch || '')})</dd>` : ''}
         ${device.memory_total_mb ? `<dt>RAM total</dt><dd>${device.memory_total_mb} MB</dd>` : ''}
         ${device.hostname ? `<dt>Host hostname</dt><dd>${escapeHtml(device.hostname)}</dd>` : ''}
         ${device.public_ip ? `<dt>Public IP</dt><dd>${escapeHtml(device.public_ip)}</dd>` : ''}
       </dl>`
    : '<div class="empty">žádná data</div>';

  const logs = (d.logs || []).map(e => {
    const cls = e.event.endsWith('_on') ? 'ev-on' : e.event.endsWith('_off') ? 'ev-off' : '';
    const note = e.note ? ' — ' + e.note : '';
    return `<div class="log-row"><span class="ts">${e.ts.replace('T',' ')}</span><span class="${cls}">${e.event}</span><span>${e.source}${escapeHtml(note)}</span></div>`;
  }).join('') || '<div class="empty">žádné události</div>';

  const dashboardUrl = `http://${d.hostname}:${d.port}/`;
  const errorNote = !reachable && d.error ? `<div class="error">${escapeHtml(d.error)}</div>` : '';

  return `
    <div class="card ${cardClass}">
      <div class="top">
        <div class="dot ${dotClass}"></div>
        <div class="identity">
          <div class="title">${escapeHtml(d.display_name)}</div>
          <div class="host">${escapeHtml(d.hostname)}:${d.port} · <a class="ext" href="${dashboardUrl}" target="_blank">otevřít ↗</a></div>
          ${location}
        </div>
        <div>
          <div class="state-label ${labelClass}">${label}</div>
          <div style="text-align:right;margin-top:.35rem">${healthBadge(d)}</div>
        </div>
      </div>

      <div class="meta">${changed}</div>
      ${errorNote}
      ${issueChips}

      <div class="metrics">
        ${internetCell}
        ${upCell}
        ${diskCell}
        ${tempCell}
        ${memCell}
        ${loadCell}
      </div>

      <div class="actions">
        <button class="on" onclick="rpiCall('${d.hostname}','on',this)" ${reachable ? '' : 'disabled'}>ON</button>
        <button class="off" onclick="rpiCall('${d.hostname}','off',this)" ${reachable ? '' : 'disabled'}>OFF</button>
        <button onclick="rpiCall('${d.hostname}','toggle',this)" ${reachable ? '' : 'disabled'}>Toggle</button>
        <button class="restart" onclick="confirmRestart('${d.hostname}','${escapeHtml(d.display_name)}',this)" ${reachable ? '' : 'disabled'} title="Restartovat celé RPi (reboot ~30-60 s)">↻</button>
      </div>

      <details data-id="device-${escapeHtml(d.hostname)}">
        <summary>Info o zařízení</summary>
        ${deviceInfo}
      </details>
      <details data-id="log-${escapeHtml(d.hostname)}">
        <summary>Log (${(d.logs || []).length})</summary>
        ${logs}
      </details>
    </div>
  `;
}

async function rpiCall(hostname, action, btn) {
  btn.disabled = true;
  try {
    const r = await fetch(`/api/rpi/${encodeURIComponent(hostname)}/${action}`, { method: 'POST' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(`Chyba: ${err.error || r.statusText}`);
    }
  } catch (e) {
    alert('Network error: ' + e);
  } finally {
    await refresh();
    btn.disabled = false;
  }
}

async function confirmRestart(hostname, name, btn) {
  if (!confirm(`Restartovat zařízení "${name}"?\n\nCelé RPi se restartuje — bude ~30-60 s nedostupné, relé zůstane OFF.`)) return;
  btn.disabled = true;
  try {
    const r = await fetch(`/api/rpi/${encodeURIComponent(hostname)}/restart`, { method: 'POST' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(`Chyba: ${err.error || r.statusText}`);
    }
  } catch (e) {
    alert('Network error: ' + e);
  } finally {
    setTimeout(() => { btn.disabled = false; refresh(); }, 15000);
  }
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  })[c]);
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    log.info("Loaded %d RPi(s) from %s", len(RPIS), RPIS_FILE)
    if HUB_ADMIN_PASSWORD or HUB_API_TOKEN:
        log.info("Auth enabled (basic=%s, bearer=%s, public bypass: %s)",
                 bool(HUB_ADMIN_PASSWORD), bool(HUB_API_TOKEN), sorted(_PUBLIC_PATHS))
    else:
        log.warning("Neither HUB_ADMIN_PASSWORD nor HUB_API_TOKEN set — hub is OPEN.")
    Thread(target=poller_loop, daemon=True).start()
    app.run(host=HOST, port=PORT)
