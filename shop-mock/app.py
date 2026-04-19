"""Trafika shop-mock — stand-in for Odoo. Role-based login that drives the relay via the hub."""
import logging
import os
import secrets
import sys
import time
from functools import wraps
from threading import Lock, Thread

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
from threading import Timer

HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:8080").rstrip("/")
# Public-facing hub URL shown in browser links (admin "open hub" button etc.).
# Internal HUB_URL is for server-side API calls (hub_post, hub_validate_qr) and stays
# on the tailnet; HUB_PUBLIC_URL is what the customer's / admin's browser must reach.
HUB_PUBLIC_URL = os.environ.get("HUB_PUBLIC_URL", HUB_URL).rstrip("/")
# Bearer token for authenticating against the hub's protected control endpoints.
# Must match HUB_API_TOKEN on the hub. Empty = no auth header sent (only works
# when the hub is also unauthenticated).
HUB_API_TOKEN = os.environ.get("HUB_API_TOKEN", "").strip()
_HUB_HEADERS = {"Authorization": f"Bearer {HUB_API_TOKEN}"} if HUB_API_TOKEN else {}
# Optional fallback for admin logins without a QR. Customers (verified/unverified)
# always need a QR-sourced pinned_rpi — knowing which machine to power on must come
# from the physical QR scan, not from shop config.
DEFAULT_RPI_HOSTNAME = os.environ.get("RPI_HOSTNAME", "").strip()

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    logging.warning("SESSION_SECRET not set — using ephemeral secret (sessions lost on restart)")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8081"))

# How long we wait without a liveness heartbeat before dropping the session.
# Liveness pings come automatically every ~10 s as long as the browser is alive,
# so this catches: tab closed, crash, sleep, network drop.
HEARTBEAT_TIMEOUT_SECONDS = int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "30"))
# Initial countdown timer length granted on login. After this expires the relay
# turns off unless the user has explicitly clicked "extend".
SESSION_DURATION_SECONDS = int(os.environ.get("SESSION_DURATION_SECONDS", "60"))
# How many seconds each "Prodloužit" click adds. Capped at MAX_SESSION_SECONDS total.
EXTEND_SECONDS = int(os.environ.get("EXTEND_SECONDS", "30"))
# Hard cap on a single session — relay always goes off at this point even if the user keeps extending.
MAX_SESSION_SECONDS = int(os.environ.get("MAX_SESSION_SECONDS", "900"))
# JS liveness polling interval (sent to the template).
HEARTBEAT_CLIENT_INTERVAL_MS = 10_000

# Hardcoded mock users. Real impl will use Odoo/external identity.
USERS = {
    "admin":      {"password": "admin",      "role": "admin",      "display": "Admin"},
    "verified":   {"password": "verified",   "role": "verified",   "display": "Ověřený zákazník"},
    "unverified": {"password": "unverified", "role": "unverified", "display": "Neověřený zákazník"},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shop-mock")

app = Flask(__name__)
app.secret_key = SESSION_SECRET


def current_user():
    uid = session.get("user_id")
    if uid in USERS:
        return {"id": uid, **USERS[uid]}
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def is_verified(user):
    if user["role"] == "verified":
        return True
    if user["role"] == "unverified" and session.get("verified_flag"):
        return True
    return False


# ----- Session presence registry -----
# Tracks live browser sessions so we can turn the relay off when the last one disappears
# (tab closed, device sleep, network drop, idle user). Heartbeat extends the lifetime;
# reaper thread prunes stale entries every few seconds.

_active = {}  # sid -> {"user_id", "role", "rpi_hostname", "last_alive", "expires_at", "started_at"}
_drop_history = {}  # sid -> timestamp of when it was evicted (reaper or beacon)
_active_lock = Lock()

# When the last session for a given RPi disappears we wait GRACE_SECONDS before turning
# its relay off. Per-RPi tracking matters because multiple sessions on different RPis
# don't interact: dropping rpi-A shouldn't power down rpi-B.
GRACE_SECONDS = 3
_pending_off_timers = {}  # rpi_hostname -> Timer
_pending_off_lock = Lock()


def _has_active_session_for(rpi_hostname):
    with _active_lock:
        return any(e.get("rpi_hostname") == rpi_hostname for e in _active.values())


def _schedule_relay_off(rpi_hostname):
    if not rpi_hostname:
        return

    def fire():
        if _has_active_session_for(rpi_hostname):
            return  # someone reactivated within the grace window
        log.info("Grace elapsed for %s — turning relay off", rpi_hostname)
        hub_post("/off", rpi_hostname=rpi_hostname)

    with _pending_off_lock:
        existing = _pending_off_timers.pop(rpi_hostname, None)
        if existing is not None:
            existing.cancel()
        t = Timer(GRACE_SECONDS, fire)
        t.daemon = True
        _pending_off_timers[rpi_hostname] = t
        t.start()


def _cancel_pending_off(rpi_hostname):
    if not rpi_hostname:
        return
    with _pending_off_lock:
        t = _pending_off_timers.pop(rpi_hostname, None)
        if t is not None:
            t.cancel()


def _ensure_sid():
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_hex(16)
        session["sid"] = sid
    return sid


def _register_presence(user_id, role="", rpi_hostname=""):
    sid = _ensure_sid()
    now = time.time()
    with _active_lock:
        existing = _active.get(sid)
        started = existing["started_at"] if existing else now
        if role == "admin":
            expires_at = float("inf")
        else:
            max_expires = started + MAX_SESSION_SECONDS
            if existing and existing.get("expires_at") not in (None, float("inf")):
                expires_at = existing["expires_at"]
            else:
                expires_at = min(now + SESSION_DURATION_SECONDS, max_expires)
        _active[sid] = {
            "user_id": user_id,
            "role": role,
            "rpi_hostname": rpi_hostname,
            "last_alive": now,
            "expires_at": expires_at,
            "started_at": started,
        }
        _drop_history.pop(sid, None)
    _cancel_pending_off(rpi_hostname)


def _refresh_alive():
    sid = session.get("sid")
    if not sid:
        return False
    with _active_lock:
        entry = _active.get(sid)
        if not entry:
            return False
        entry["last_alive"] = time.time()
    return True


def _extend_session():
    sid = session.get("sid")
    if not sid:
        return None
    now = time.time()
    with _active_lock:
        entry = _active.get(sid)
        if not entry:
            return None
        if entry.get("role") == "admin":
            entry["last_alive"] = now
            return _entry_view(entry, now)
        max_expires = entry["started_at"] + MAX_SESSION_SECONDS
        new_expires = min(entry["expires_at"] + EXTEND_SECONDS, max_expires)
        entry["expires_at"] = new_expires
        entry["last_alive"] = now
        return _entry_view(entry, now)


def _entry_view(entry, now=None):
    if now is None:
        now = time.time()
    if entry.get("role") == "admin":
        return {"timer_disabled": True, "expires_in": None, "max_remaining": None, "at_max": False}
    max_expires = entry["started_at"] + MAX_SESSION_SECONDS
    return {
        "timer_disabled": False,
        "expires_in": max(0, int(round(entry["expires_at"] - now))),
        "max_remaining": max(0, int(round(max_expires - now))),
        "at_max": entry["expires_at"] >= max_expires - 0.5,
    }


def _current_session_view():
    sid = session.get("sid")
    if not sid:
        return None
    with _active_lock:
        entry = _active.get(sid)
        if not entry:
            return None
        return _entry_view(entry)


def _drop_presence():
    """Remove current session. If its RPi has no other sessions, schedule a deferred off."""
    sid = session.get("sid")
    if not sid:
        return
    with _active_lock:
        entry = _active.pop(sid, None)
        _drop_history[sid] = time.time()
    rpi = entry.get("rpi_hostname") if entry else None
    if rpi and not _has_active_session_for(rpi):
        log.info("Session %s ended for %s — scheduling relay off in %ds", sid, rpi, GRACE_SECONDS)
        _schedule_relay_off(rpi)


def _reaper_loop():
    while True:
        time.sleep(2)
        now = time.time()
        expired = []
        affected_rpis = set()
        with _active_lock:
            for sid, entry in list(_active.items()):
                reason = None
                if now - entry["last_alive"] > HEARTBEAT_TIMEOUT_SECONDS:
                    reason = "disconnected"
                elif now > entry["expires_at"]:
                    reason = "timer_expired"
                if reason:
                    expired.append((sid, reason, entry["user_id"], entry.get("rpi_hostname", "")))
                    affected_rpis.add(entry.get("rpi_hostname", ""))
                    _active.pop(sid, None)
                    _drop_history[sid] = now
            # Housekeeping: forget drop history older than 1 hour
            cutoff = now - 3600
            for sid in [s for s, t in _drop_history.items() if t < cutoff]:
                _drop_history.pop(sid, None)
        for sid, reason, user_id, rpi in expired:
            log.info("Reaper dropped session %s (user=%s, rpi=%s, reason=%s)",
                     sid, user_id, rpi, reason)
        for rpi in affected_rpis:
            if rpi and not _has_active_session_for(rpi):
                log.info("All sessions for %s gone — scheduling off in %ds", rpi, GRACE_SECONDS)
                _schedule_relay_off(rpi)


def session_rpi():
    """Which RPi this browser session is driving. QR activation pins it; admin
    sessions may fall back to DEFAULT_RPI_HOSTNAME if configured. Returns None
    when neither is available (caller must handle)."""
    return session.get("pinned_rpi") or DEFAULT_RPI_HOSTNAME or None


def hub_post(path, rpi_hostname=None):
    host = rpi_hostname or session_rpi()
    try:
        r = requests.post(f"{HUB_URL}/api/rpi/{host}{path}", headers=_HUB_HEADERS, timeout=5)
        r.raise_for_status()
        return True, None
    except requests.RequestException as e:
        log.warning("hub call failed: %s/api/rpi/%s%s — %s", HUB_URL, host, path, e)
        return False, str(e)


def hub_state(rpi_hostname=None):
    host = rpi_hostname or session_rpi()
    try:
        r = requests.get(f"{HUB_URL}/api/dashboard", headers=_HUB_HEADERS, timeout=3)
        for rpi in r.json():
            if rpi["hostname"] == host:
                return rpi
    except (requests.RequestException, ValueError):
        pass
    return None


def hub_validate_qr(hostname, token):
    try:
        # /api/qr/validate is on the hub's public bypass list, no auth header needed.
        r = requests.post(f"{HUB_URL}/api/qr/validate",
                          json={"rpi_hostname": hostname, "token": token},
                          timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("QR validate failed: %s", e)
        return {"valid": False, "reason": str(e)}


@app.get("/activate/<rpi>/<token>")
def activate_qr(rpi, token):
    result = hub_validate_qr(rpi, token)
    if not result.get("valid"):
        return render_template_string(QR_ERROR_HTML,
                                      reason=result.get("reason", "unknown"),
                                      rpi=rpi), 400
    # Preserve login + verified_flag so a returning customer doesn't have to re-authenticate
    # when they scan a fresh QR. The pin + pin timestamp are overwritten fresh.
    keep_user = session.get("user_id")
    keep_verified = session.get("verified_flag")
    session.clear()
    if keep_user:
        session["user_id"] = keep_user
    if keep_verified:
        session["verified_flag"] = keep_verified
    session["pinned_rpi"] = rpi
    session["pinned_at"] = int(time.time())
    return redirect(url_for("login"))


@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("home"))
    pinned = session.get("pinned_rpi")
    return render_template_string(LOGIN_HTML, error=request.args.get("error"), pinned_rpi=pinned)


@app.post("/login")
def do_login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = USERS.get(username)
    if not user or user["password"] != password:
        return redirect(url_for("login", error="bad_credentials"))
    # Login always succeeds — activation gate is separate. Without a QR-sourced pin
    # the customer just lands on the "awaiting QR" page; credentials don't grant
    # power on their own.
    pinned = session.get("pinned_rpi")
    pinned_at = session.get("pinned_at")
    verified_flag = session.get("verified_flag")
    session.clear()
    session["user_id"] = username
    if pinned:
        session["pinned_rpi"] = pinned
        if pinned_at:
            session["pinned_at"] = pinned_at
    if verified_flag:
        session["verified_flag"] = verified_flag
    return redirect(url_for("home"))


@app.post("/logout")
@login_required
def logout():
    _drop_presence()
    session.clear()
    return redirect(url_for("login"))


@app.post("/session/heartbeat")
def session_heartbeat():
    if not _refresh_alive():
        return jsonify(ok=False, reason="no active session"), 410
    view = _current_session_view()
    return jsonify(ok=True, **(view or {})), 200


@app.post("/session/extend")
@login_required
def session_extend():
    view = _extend_session()
    if view is None:
        return jsonify(ok=False, reason="no active session"), 410
    return jsonify(ok=True, **view), 200


@app.post("/session/end")
def session_end():
    # Hit via navigator.sendBeacon on pagehide. We do NOT clear the browser session here
    # because same-origin navigation (form submit, internal link) also fires pagehide —
    # clearing would log the user out mid-navigation. The grace window on _drop_presence
    # gives the follow-up request a chance to re-register before the relay turns off.
    _drop_presence()
    return ("", 204)


@app.after_request
def _no_store_home(resp):
    # Browser BACK on a cached home would skip our activation checks. Force re-fetch.
    if request.path == "/" and request.method == "GET":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
@login_required
def home():
    user = current_user()

    if user["role"] == "unverified" and not session.get("verified_flag"):
        return redirect(url_for("verify"))

    # Admin may have logged in without pinning a specific RPi — show an info page
    # instead of blindly targeting a machine.
    if user["role"] == "admin" and not session_rpi():
        return render_template_string(ADMIN_NO_RPI_HTML, user=user, hub_url=HUB_PUBLIC_URL)

    # Customers must have a *fresh* QR-sourced pin. A cookie lingering from hours ago
    # isn't good enough — force a re-scan.
    if user["role"] != "admin":
        pinned_rpi = session.get("pinned_rpi")
        pinned_at = session.get("pinned_at", 0)
        now = time.time()
        if not pinned_rpi or (now - pinned_at) > MAX_SESSION_SECONDS:
            session.pop("pinned_rpi", None)
            session.pop("pinned_at", None)
            return redirect(url_for("expired"))
        # If this session was previously dropped (reaper expired it, or beacon fired
        # and grace lapsed), block the re-activation. Distinguishes "browser back after
        # expiry" (stale sid, long-ago drop) from "mid-flow form navigation" (grace window).
        sid = session.get("sid")
        if sid:
            with _active_lock:
                dropped_at = None if sid in _active else _drop_history.get(sid)
            if dropped_at is not None and (now - dropped_at) > GRACE_SECONDS + 2:
                session.pop("pinned_rpi", None)
                session.pop("pinned_at", None)
                return redirect(url_for("expired"))

    _register_presence(user["id"], user["role"], rpi_hostname=session_rpi() or "")
    view = _current_session_view() or {"timer_disabled": False, "expires_in": 0, "max_remaining": 0, "at_max": False}

    relay_error = None
    if user["role"] in ("verified", "unverified"):
        ok, err = hub_post("/on")
        if not ok:
            relay_error = err

    return render_template_string(HOME_HTML,
                                  user=user,
                                  state=hub_state(),
                                  relay_error=relay_error,
                                  rpi_hostname=session_rpi(),
                                  pinned_via_qr=bool(session.get("pinned_rpi")),
                                  heartbeat_ms=HEARTBEAT_CLIENT_INTERVAL_MS,
                                  heartbeat_timeout=HEARTBEAT_TIMEOUT_SECONDS,
                                  session_duration=SESSION_DURATION_SECONDS,
                                  extend_seconds=EXTEND_SECONDS,
                                  max_session_seconds=MAX_SESSION_SECONDS,
                                  expires_in=view.get("expires_in") or 0,
                                  max_remaining=view.get("max_remaining") or 0,
                                  at_max=view.get("at_max", False),
                                  timer_disabled=view.get("timer_disabled", False))


@app.get("/verify")
@login_required
def verify():
    user = current_user()
    if user["role"] != "unverified" or session.get("verified_flag"):
        return redirect(url_for("home"))
    return render_template_string(VERIFY_HTML, user=user)


@app.get("/expired")
@login_required
def expired():
    return render_template_string(EXPIRED_HTML, user=current_user())


@app.post("/verify")
@login_required
def do_verify():
    user = current_user()
    if user["role"] != "unverified":
        return redirect(url_for("home"))
    session["verified_flag"] = True
    return redirect(url_for("home"))


@app.post("/relay/on")
@login_required
def relay_on():
    user = current_user()
    if user["role"] != "admin" and not is_verified(user):
        return jsonify(ok=False, error="account not verified"), 403
    hub_post("/on")
    return redirect(url_for("home"))


@app.post("/relay/off")
@login_required
def relay_off():
    hub_post("/off")
    return redirect(url_for("home"))


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, service="trafika-shop-mock")


# ----- templates -----

BASE_CSS = """
  :root { --on:#22c55e; --off:#ef4444; --bg:#0f172a; --fg:#e2e8f0; --muted:#64748b; --card:#1e293b; --warn:#f59e0b; --accent:#60a5fa; --border:#334155; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--fg); min-height: 100vh; display: flex; flex-direction: column; align-items: center; padding: 3rem 1.5rem; }
  main { width: 100%; max-width: 520px; }
  h1 { margin: 0 0 .5rem; font-size: 1.6rem; }
  .tag { color: var(--muted); font-size: .85rem; letter-spacing: .1em; text-transform: uppercase; margin-bottom: 2rem; }
  .card { background: var(--card); border-radius: 1rem; padding: 2rem; border: 1px solid var(--border); }
  .card + .card { margin-top: 1rem; }
  label { display: block; font-size: .85rem; color: var(--muted); margin-bottom: .3rem; margin-top: 1rem; }
  input { width: 100%; background: #0f172a; border: 1px solid var(--border); color: var(--fg); padding: .7rem .9rem; border-radius: .5rem; font-size: 1rem; }
  input:focus { outline: none; border-color: var(--accent); }
  button { background: var(--accent); color: #0b1a2e; border: none; padding: .8rem 1.2rem; border-radius: .5rem; font-size: 1rem; cursor: pointer; font-weight: 600; }
  button:hover { filter: brightness(1.1); }
  button.secondary { background: #334155; color: var(--fg); }
  button.ok { background: var(--on); color: #052e16; }
  button.warn { background: var(--off); color: #450a0a; }
  .full { width: 100%; margin-top: 1rem; }
  .error { background: rgba(239,68,68,.15); color: #fca5a5; padding: .6rem .9rem; border-radius: .5rem; margin-bottom: 1rem; font-size: .9rem; }
  .muted { color: var(--muted); font-size: .85rem; }
  a { color: var(--accent); text-decoration: none; }
  .dot { width: 3rem; height: 3rem; border-radius: 50%; display: inline-block; vertical-align: middle; margin-right: 1rem; }
  .dot.on { background: var(--on); box-shadow: 0 0 25px var(--on); }
  .dot.off { background: var(--off); opacity: .75; }
  .dot.unknown { background: var(--muted); }
  .status-line { display: flex; align-items: center; gap: 1rem; font-size: 1.4rem; font-weight: 600; }
  .status-line.on { color: var(--on); }
  .status-line.off { color: var(--off); }
  .badge { display: inline-block; padding: .15rem .55rem; border-radius: .4rem; font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
  .badge.verified { background: rgba(34,197,94,.18); color: var(--on); }
  .badge.unverified { background: rgba(245,158,11,.18); color: var(--warn); }
  .badge.admin { background: rgba(96,165,250,.18); color: var(--accent); }
  .actions { display: flex; gap: .6rem; margin-top: 1.5rem; }
  form.inline { display: inline; }
  .preset { margin-top: 1.5rem; padding: 1rem; background: #0f172a; border: 1px dashed var(--border); border-radius: .5rem; font-size: .82rem; }
  .preset h2 { font-size: .75rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin: 0 0 .6rem; font-weight: 500; }
  .preset-row { display: flex; justify-content: space-between; padding: .35rem 0; border-bottom: 1px solid var(--border); font-family: ui-monospace, monospace; font-size: .78rem; }
  .preset-row:last-child { border: 0; }
  .preset-row .use { color: var(--accent); cursor: pointer; }
  .timer { text-align: center; padding: 1.2rem 0 .4rem; }
  .timer .label { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .1em; margin-bottom: .4rem; }
  .timer .value { font-size: 3rem; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; }
  .timer.urgent .value { color: var(--off); animation: pulse 1s ease-in-out infinite; }
  .timer .max { color: var(--muted); font-size: .8rem; margin-top: .5rem; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .55; } }
"""

LOGIN_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Přihlášení — Trafika</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>""" + BASE_CSS + """</style>
</head>
<body>
<main>
  <h1>Trafika — přihlášení</h1>
  <div class="tag">MOCK shop (stand-in za Odoo)</div>

  <div class="card">
    {% if pinned_rpi %}<div style="background:rgba(96,165,250,.15);color:var(--accent);padding:.6rem .9rem;border-radius:.5rem;margin-bottom:1rem;font-size:.9rem">
      Aktivace přes QR pro <strong>{{ pinned_rpi }}</strong>. Po přihlášení se automat zapne.
    </div>{% endif %}
    {% if error == 'bad_credentials' %}<div class="error">Špatné uživatelské jméno nebo heslo.</div>
    {% elif error %}<div class="error">Nepovedlo se přihlásit.</div>{% endif %}
    <form method="post" action="/login">
      <label for="u">Uživatel</label>
      <input id="u" name="username" autocomplete="username" autofocus>
      <label for="p">Heslo</label>
      <input id="p" name="password" type="password" autocomplete="current-password">
      <button class="full" type="submit">Přihlásit se</button>
    </form>

    <div class="preset">
      <h2>Testovací účty</h2>
      <div class="preset-row"><span>admin / admin</span><span class="use" onclick="fill('admin')">vyplnit</span></div>
      <div class="preset-row"><span>verified / verified</span><span class="use" onclick="fill('verified')">vyplnit</span></div>
      <div class="preset-row"><span>unverified / unverified</span><span class="use" onclick="fill('unverified')">vyplnit</span></div>
    </div>
  </div>
</main>
<script>
function fill(name) {
  document.getElementById('u').value = name;
  document.getElementById('p').value = name;
  document.getElementById('p').focus();
}
</script>
</body>
</html>
"""

HOME_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Trafika — {{ user.display }}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>""" + BASE_CSS + """</style>
</head>
<body>
<main>
  <h1>Ahoj, {{ user.display }}</h1>
  <div class="tag">
    {% if user.role == 'admin' %}<span class="badge admin">admin</span>
    {% elif user.role == 'verified' %}<span class="badge verified">ověřený</span>
    {% else %}<span class="badge verified">ověřený (mock)</span>{% endif %}
    &nbsp; · &nbsp; automat: {{ rpi_hostname }}{% if pinned_via_qr %} <span class="badge admin">přes QR</span>{% endif %}
  </div>

  {% if relay_error %}<div class="error">Nepodařilo se zapnout automat přes hub: {{ relay_error }}</div>{% endif %}

  {% set on = state and state.state and state.state.relay == 'ON' %}
  {% set reachable = state and state.reachable %}

  <div class="card">
    <div class="status-line {{ 'on' if on else ('off' if reachable else '') }}">
      <span class="dot {{ 'on' if on else ('off' if reachable else 'unknown') }}"></span>
      {% if on %}Automat BĚŽÍ
      {% elif reachable %}Automat vypnutý
      {% else %}Automat offline{% endif %}
    </div>
    {% if state and state.state and state.state.changed_at %}
      <div class="muted" style="margin-top:.5rem">Poslední změna: {{ state.state.changed_at }} · zdroj: {{ state.state.changed_by or '—' }}</div>
    {% endif %}

    {% if user.role == 'admin' %}
      <div class="actions">
        <form class="inline" method="post" action="/relay/on"><button class="ok" type="submit">Zapnout</button></form>
        <form class="inline" method="post" action="/relay/off"><button class="warn" type="submit">Vypnout</button></form>
      </div>
      <div class="muted" style="margin-top:1rem">Jako admin máš plnou kontrolu — žádná kontrola ověření.</div>
    {% else %}
      <div class="muted" style="margin-top:1rem">
        {% if on %}Automat je pro tebe k dispozici. Můžeš nakupovat.
        {% else %}Automat se právě zapíná — osvěž stránku za pár sekund.{% endif %}
      </div>
    {% endif %}
  </div>

  {% if not timer_disabled %}
  <div class="card">
    <div class="timer" id="timer" data-expires-in="{{ expires_in }}" data-max-remaining="{{ max_remaining }}" data-at-max="{{ 'true' if at_max else 'false' }}">
      <div class="label">Čas do vypnutí</div>
      <div class="value" id="timer-value">—</div>
      <div class="max" id="timer-max"></div>
    </div>
    <button class="full ok" id="extend-btn" type="button" onclick="extendSession()">
      Prodloužit o {% if extend_seconds >= 60 %}{{ (extend_seconds / 60) | int }} min{% else %}{{ extend_seconds }} s{% endif %}
    </button>
  </div>
  {% endif %}

  <div class="card">
    <div class="muted" style="margin-bottom:.8rem">
      {% if timer_disabled %}
        <strong>Admin mode</strong> — žádný odpočet. Relé se vypne jen ručně nebo při zavření prohlížeče.
      {% else %}
        Automat se vypne při vypršení časovače, zavření prohlížeče, nebo nejpozději po {{ (max_session_seconds / 60) | int }} min od přihlášení.
      {% endif %}
    </div>
    <form method="post" action="/logout" style="margin:0">
      <button class="secondary" type="submit">Odhlásit (automat se vypne)</button>
    </form>
  </div>
</main>
<script>
(function () {
  const LIVENESS_INTERVAL = {{ heartbeat_ms }};
  const timerEl = document.getElementById('timer');

  // Liveness ping — always runs, catches tab close / disconnect even for admin.
  function liveness() {
    fetch('/session/heartbeat', { method: 'POST', credentials: 'same-origin' })
      .then(r => { if (r.status === 410) window.location.href = '/expired'; })
      .catch(() => {});
  }
  liveness();
  setInterval(liveness, LIVENESS_INTERVAL);
  window.addEventListener('pagehide', () => navigator.sendBeacon('/session/end'));

  // Timer + extend logic only runs if the timer card was rendered (non-admin sessions).
  if (!timerEl) return;

  const valueEl = document.getElementById('timer-value');
  const maxEl = document.getElementById('timer-max');
  const extendBtn = document.getElementById('extend-btn');

  let expiresIn = parseInt(timerEl.dataset.expiresIn || '0', 10);
  let maxRemaining = parseInt(timerEl.dataset.maxRemaining || '0', 10);
  let atMax = timerEl.dataset.atMax === 'true';

  function fmt(s) {
    if (s < 0) s = 0;
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return m + ':' + (sec < 10 ? '0' : '') + sec;
  }

  function render() {
    valueEl.textContent = fmt(expiresIn);
    timerEl.classList.toggle('urgent', expiresIn <= 30);
    maxEl.textContent = atMax
      ? 'Dosažen maximální čas — nelze prodloužit, zbývá ' + fmt(maxRemaining)
      : 'Max v této session: ' + fmt(maxRemaining);
    extendBtn.disabled = atMax || maxRemaining <= 0;
  }

  function tick() {
    if (expiresIn > 0) expiresIn -= 1;
    if (maxRemaining > 0) maxRemaining -= 1;
    render();
    if (expiresIn <= 0) {
      fetch('/session/heartbeat', { method: 'POST', credentials: 'same-origin' })
        .then(r => { if (r.status === 410) window.location.href = '/expired'; });
    }
  }

  async function extendSession() {
    extendBtn.disabled = true;
    try {
      const r = await fetch('/session/extend', { method: 'POST', credentials: 'same-origin' });
      if (r.status === 410) { window.location.href = '/expired'; return; }
      const data = await r.json();
      expiresIn = data.expires_in;
      maxRemaining = data.max_remaining;
      atMax = !!data.at_max;
      render();
    } catch (e) {
      render();
    }
  }
  window.extendSession = extendSession;

  render();
  setInterval(tick, 1000);
})();
</script>
</body>
</html>
"""

EXPIRED_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Naskenuj QR — Trafika</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>""" + BASE_CSS + """</style>
</head>
<body>
<main>
  <h1>Přihlášen jako {{ user.display }}</h1>
  <div class="tag">žádný automat aktuálně napárovaný</div>

  <div class="card">
    <p><strong>Naskenuj QR kód přímo u automatu</strong>, který chceš použít.</p>
    <p class="muted" style="margin-top:1rem">Přihlášení je platné — po naskenování QR se automat aktivuje rovnou, bez dalšího přihlašování.</p>
  </div>

  <div class="card">
    <form method="post" action="/logout" style="margin:0">
      <button class="secondary" type="submit">Odhlásit</button>
    </form>
  </div>
</main>
</body>
</html>
"""


ADMIN_NO_RPI_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Admin — Trafika</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>""" + BASE_CSS + """</style>
</head>
<body>
<main>
  <h1>Ahoj, {{ user.display }}</h1>
  <div class="tag"><span class="badge admin">admin</span></div>

  <div class="card">
    <p>Přihlásil ses jako admin, ale nejsi napárovaný s konkrétním automatem — neproběhla QR aktivace a `RPI_HOSTNAME` v shop-mock `.env` není nastavený.</p>
    <p class="muted">Pro správu všech zařízení použij hub dashboard. Zde v shop-mocku se dá nasimulovat „zákaznické" přihlášení jen po naskenování QR konkrétního automatu.</p>
    <a href="{{ hub_url }}/" class="full" style="display:block;text-align:center;padding:.8rem;background:var(--accent);color:#0b1a2e;border-radius:.5rem;text-decoration:none;font-weight:600;margin-top:1rem">Otevřít hub dashboard ↗</a>
  </div>

  <div class="card">
    <form method="post" action="/logout" style="margin:0">
      <button class="secondary" type="submit">Odhlásit</button>
    </form>
  </div>
</main>
</body>
</html>
"""


QR_ERROR_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>QR neplatný — Trafika</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>""" + BASE_CSS + """</style>
</head>
<body>
<main>
  <h1>QR kód je neplatný</h1>
  <div class="tag">automat: {{ rpi }}</div>
  <div class="card">
    <div class="error">Důvod: {{ reason }}</div>
    <p class="muted" style="margin-top:1rem">Nejčastější důvod — QR vypršel (rotuje se každých 60 s). Naskenuj čerstvý kód přímo u automatu.</p>
  </div>
</main>
</body>
</html>
"""


VERIFY_HTML = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Ověření účtu — Trafika</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>""" + BASE_CSS + """</style>
</head>
<body>
<main>
  <h1>Ověření účtu</h1>
  <div class="tag"><span class="badge unverified">neověřený účet</span></div>

  <div class="card">
    <p>Účet <strong>{{ user.display }}</strong> ještě nebyl ověřený. Pro použití automatu je potřeba ověření.</p>
    <p class="muted">(V reálné implementaci by zde byla kontrola dokladu totožnosti, věku apod. Teď je to jen mock — klikni na tlačítko.)</p>
    <form method="post" action="/verify">
      <button class="full ok" type="submit">Ověřit účet (MOCK)</button>
    </form>
  </div>

  <div class="card">
    <form method="post" action="/logout" style="margin:0">
      <button class="secondary" type="submit">Odhlásit</button>
    </form>
  </div>
</main>
</body>
</html>
"""


if __name__ == "__main__":
    log.info("Starting shop-mock on %s:%s — hub=%s rpi=%s liveness=%ds session=%ds extend=%ds max=%ds",
             HOST, PORT, HUB_URL, DEFAULT_RPI_HOSTNAME,
             HEARTBEAT_TIMEOUT_SECONDS, SESSION_DURATION_SECONDS, EXTEND_SECONDS, MAX_SESSION_SECONDS)
    Thread(target=_reaper_loop, daemon=True).start()
    app.run(host=HOST, port=PORT)
