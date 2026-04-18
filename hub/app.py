"""Trafika central hub — aggregates state across all RPi controllers."""
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

import requests
import yaml
from flask import Flask, abort, jsonify, render_template_string

RPIS_FILE = Path(os.environ.get("RPIS_FILE", "/config/rpis.yml"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT", "4"))
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


def poll_one(rpi):
    host = rpi["hostname"]
    entry = {
        "reachable": False,
        "state": None,
        "logs": [],
        "last_check": datetime.now().isoformat(timespec="seconds"),
        "error": None,
    }
    try:
        s = requests.get(rpi_url(rpi, "/api/state"), timeout=POLL_TIMEOUT)
        s.raise_for_status()
        entry["state"] = s.json()
        l = requests.get(rpi_url(rpi, "/api/logs"), timeout=POLL_TIMEOUT)
        l.raise_for_status()
        entry["logs"] = l.json()[:20]
        entry["reachable"] = True
    except requests.RequestException as e:
        entry["error"] = str(e)
    with _cache_lock:
        _cache[host] = entry


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


@app.get("/api/dashboard")
def api_dashboard():
    with _cache_lock:
        result = []
        for r in RPIS:
            c = _cache.get(r["hostname"], {
                "reachable": False, "state": None, "logs": [],
                "last_check": None, "error": "not polled yet",
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
  :root { --on:#22c55e; --off:#ef4444; --bg:#0f172a; --fg:#e2e8f0; --muted:#64748b; --card:#1e293b; --warn:#f59e0b; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--fg); padding: 2rem; max-width: 1400px; margin: 0 auto; }
  header { display:flex; align-items:baseline; justify-content: space-between; margin-bottom: 2rem; }
  h1 { margin: 0; font-size: 1.25rem; font-weight: 600; letter-spacing: .02em; }
  .updated { color: var(--muted); font-size: .85rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 1.25rem; }
  .card { background: var(--card); border-radius: 1rem; padding: 1.5rem; display: flex; flex-direction: column; gap: 1rem; }
  .card.offline { opacity: .55; border: 1px solid var(--warn); }
  .top { display: flex; align-items: center; gap: 1rem; }
  .dot { width: 3rem; height: 3rem; border-radius: 50%; flex-shrink: 0; }
  .dot.on { background: var(--on); box-shadow: 0 0 24px var(--on); }
  .dot.off { background: var(--off); opacity: .7; }
  .dot.unknown { background: var(--muted); }
  .title { font-weight: 600; font-size: 1.05rem; }
  .host { color: var(--muted); font-size: .8rem; font-family: ui-monospace, monospace; }
  .state-label { font-weight: 700; }
  .state-label.on { color: var(--on); }
  .state-label.off { color: var(--off); }
  .meta { color: var(--muted); font-size: .85rem; }
  .meta .tag { display: inline-block; background:#334155; color: var(--fg); padding: .1rem .5rem; border-radius: .35rem; font-size: .7rem; margin-left: .3rem; }
  .offline-tag { color: var(--warn); font-weight: 600; }
  .actions { display: flex; gap: .5rem; }
  button { flex: 1; background: #334155; color: var(--fg); border: none; padding: .6rem; border-radius: .5rem; font-size: .9rem; cursor: pointer; font-weight: 500; }
  button:hover:not(:disabled) { background: #475569; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  button.on { background: #166534; }
  button.on:hover:not(:disabled) { background: #15803d; }
  button.off { background: #7f1d1d; }
  button.off:hover:not(:disabled) { background: #991b1b; }
  details { font-size: .8rem; }
  details summary { cursor: pointer; color: var(--muted); padding: .3rem 0; }
  .log-row { padding: .3rem 0; border-bottom: 1px solid #334155; display: grid; grid-template-columns: 9rem 7rem 1fr; gap: .5rem; font-family: ui-monospace, monospace; font-size: .75rem; }
  .log-row:last-child { border: 0; }
  .log-row .ts { color: var(--muted); }
  .ev-on { color: var(--on); }
  .ev-off { color: var(--off); }
  .empty { color: var(--muted); font-style: italic; padding: .5rem 0; }
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
async function refresh() {
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
}

function renderCard(d) {
  const state = d.state && d.state.relay;
  const reachable = d.reachable;
  const on = state === 'ON';
  const dotClass = !reachable ? 'unknown' : (on ? 'on' : 'off');
  const labelClass = !reachable ? '' : (on ? 'on' : 'off');
  const label = reachable ? state : '—';
  const changed = d.state && d.state.changed_at
    ? `Změněno ${d.state.changed_at}${d.state.changed_by ? ' · ' + d.state.changed_by : ''}`
    : 'Zatím žádná změna';
  const offlineNote = reachable ? '' : `<div class="offline-tag">OFFLINE</div><div class="error">${escapeHtml(d.error || '')}</div>`;
  const logs = (d.logs || []).map(e => {
    const cls = e.event.endsWith('_on') ? 'ev-on' : e.event.endsWith('_off') ? 'ev-off' : '';
    const note = e.note ? ' — ' + e.note : '';
    return `<div class="log-row"><span class="ts">${e.ts.replace('T',' ')}</span><span class="${cls}">${e.event}</span><span>${e.source}${escapeHtml(note)}</span></div>`;
  }).join('') || '<div class="empty">žádné události</div>';

  const dashboardUrl = `http://${d.hostname}:${d.port}/`;
  return `
    <div class="card${reachable ? '' : ' offline'}">
      <div class="top">
        <div class="dot ${dotClass}"></div>
        <div style="flex:1;min-width:0">
          <div class="title">${escapeHtml(d.display_name)}</div>
          <div class="host">${escapeHtml(d.hostname)}:${d.port} · <a class="ext" href="${dashboardUrl}" target="_blank">otevřít ↗</a></div>
        </div>
        <div class="state-label ${labelClass}">${label}</div>
      </div>
      <div class="meta">${changed}${reachable ? '' : ' ' + offlineNote}</div>
      <div class="actions">
        <button class="on" onclick="rpiCall('${d.hostname}','on',this)" ${reachable ? '' : 'disabled'}>ON</button>
        <button class="off" onclick="rpiCall('${d.hostname}','off',this)" ${reachable ? '' : 'disabled'}>OFF</button>
        <button onclick="rpiCall('${d.hostname}','toggle',this)" ${reachable ? '' : 'disabled'}>Toggle</button>
      </div>
      <details>
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
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
    Thread(target=poller_loop, daemon=True).start()
    app.run(host=HOST, port=PORT)
