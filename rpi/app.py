"""Trafika vending controller — webhook receiver + live dashboard (env-configured)."""
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock

from flask import Flask, abort, jsonify, render_template_string, request

WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "").strip()
if not WEBHOOK_TOKEN:
    print("FATAL: WEBHOOK_TOKEN env var is required", file=sys.stderr)
    sys.exit(1)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DEVICE_NAME = os.environ.get("DEVICE_NAME", socket.gethostname())
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = DATA_DIR / "events.log"

app = Flask(__name__)

_state = {"relay": "OFF", "changed_at": None, "changed_by": None}
_state_lock = Lock()


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


@app.get("/api/state")
def api_state():
    return jsonify(_state_payload())


@app.get("/api/logs")
def api_logs():
    return jsonify(tail_log(100))


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, device=DEVICE_NAME)


@app.post("/ui/toggle")
def ui_toggle():
    target = "ON" if _state["relay"] == "OFF" else "OFF"
    set_relay(target, source="dashboard")
    return jsonify(ok=True, state=_state_payload())


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
    log_event("service_start", source="system", note=f"device={DEVICE_NAME} default_state={_state['relay']}")
    app.run(host=HOST, port=PORT)
