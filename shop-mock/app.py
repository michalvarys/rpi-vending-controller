"""Trafika shop-mock — stand-in for Odoo. Role-based login that drives the relay via the hub."""
import logging
import os
import secrets
import sys
from functools import wraps

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for

HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:8080").rstrip("/")
RPI_HOSTNAME = os.environ.get("RPI_HOSTNAME", "").strip()
if not RPI_HOSTNAME:
    print("FATAL: RPI_HOSTNAME env var is required", file=sys.stderr)
    sys.exit(1)

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    logging.warning("SESSION_SECRET not set — using ephemeral secret (sessions lost on restart)")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8081"))

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


def hub_post(path):
    try:
        r = requests.post(f"{HUB_URL}/api/rpi/{RPI_HOSTNAME}{path}", timeout=5)
        r.raise_for_status()
        return True, None
    except requests.RequestException as e:
        log.warning("hub call failed: %s%s — %s", HUB_URL, path, e)
        return False, str(e)


def hub_state():
    try:
        for rpi in requests.get(f"{HUB_URL}/api/dashboard", timeout=3).json():
            if rpi["hostname"] == RPI_HOSTNAME:
                return rpi
    except (requests.RequestException, ValueError):
        pass
    return None


@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("home"))
    return render_template_string(LOGIN_HTML, error=request.args.get("error"))


@app.post("/login")
def do_login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = USERS.get(username)
    if not user or user["password"] != password:
        return redirect(url_for("login", error="bad_credentials"))
    session.clear()
    session["user_id"] = username
    return redirect(url_for("home"))


@app.post("/logout")
@login_required
def logout():
    user = current_user()
    if is_verified(user) or user["role"] == "admin":
        hub_post("/off")
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    user = current_user()

    if user["role"] == "unverified" and not session.get("verified_flag"):
        return redirect(url_for("verify"))

    relay_error = None
    if user["role"] in ("verified", "unverified"):
        ok, err = hub_post("/on")
        if not ok:
            relay_error = err

    return render_template_string(HOME_HTML,
                                  user=user,
                                  state=hub_state(),
                                  relay_error=relay_error,
                                  rpi_hostname=RPI_HOSTNAME)


@app.get("/verify")
@login_required
def verify():
    user = current_user()
    if user["role"] != "unverified" or session.get("verified_flag"):
        return redirect(url_for("home"))
    return render_template_string(VERIFY_HTML, user=user)


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
    ok, err = hub_post("/on")
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error=err), 502)


@app.post("/relay/off")
@login_required
def relay_off():
    ok, err = hub_post("/off")
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error=err), 502)


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
    {% if error %}<div class="error">Špatné uživatelské jméno nebo heslo.</div>{% endif %}
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
    &nbsp; · &nbsp; automat: {{ rpi_hostname }}
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

  <div class="card">
    <form method="post" action="/logout" style="margin:0">
      <button class="secondary" type="submit">Odhlásit (automat se vypne)</button>
    </form>
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
    log.info("Starting shop-mock on %s:%s — hub=%s rpi=%s", HOST, PORT, HUB_URL, RPI_HOSTNAME)
    app.run(host=HOST, port=PORT)
