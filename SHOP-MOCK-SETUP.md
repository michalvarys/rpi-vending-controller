# Trafika shop-mock — nasazení na VPS

Mock přihlašovací rozhraní, které supluje Odoo: uživatel se přihlásí, a podle jeho role se buď sepne relé (zákazník s ověřeným účtem), ukáže ověřovací krok (zákazník bez ověření), nebo admin dostane ruční ovládání. Cílem je otestovat interakci Odoo ↔ hub ↔ RPi, než napojíme reálné Odoo.

Image: `ghcr.io/michalvarys/trafika-shop-mock:latest`, multiarch.

---

## 0. Prerekvizity

- VPS má běžet `trafika-hub` dle `HUB-SETUP.md`.
- Alespoň jeden RPi zaregistrovaný v `hub/rpis.yml` a reachable.
- Docker + compose plugin.

---

## 1. Příprava adresáře

```bash
sudo mkdir -p /opt/trafika-shop-mock
sudo chown $USER:$USER /opt/trafika-shop-mock
cd /opt/trafika-shop-mock
```

---

## 2. Stažení compose a `.env`

```bash
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/shop-mock/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/shop-mock/.env.example -o .env
```

Edituj `.env`:

```ini
PORT=8081
HUB_URL=http://127.0.0.1:8080
RPI_HOSTNAME=rpi-vending
SESSION_SECRET=<výstup z 'openssl rand -hex 32'>
```

Vygeneruj `SESSION_SECRET` (ať přežijí sessions restart kontejneru):

```bash
sed -i "s|^SESSION_SECRET=.*$|SESSION_SECRET=$(openssl rand -hex 32)|" .env
chmod 600 .env
```

---

## 3. Spuštění

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

Shop-mock používá `network_mode: host` — poslouchá přímo na hostovém portu 8081 (bez port mappingu), takže snadno dosáhne na hub na `127.0.0.1:8080`. Reachable z celého tailnetu jako `http://varyshop-trafika-vps:8081/`.

---

## 4. Test flow

Otevři `http://varyshop-trafika-vps:8081/` v prohlížeči. Login vidí tři testovací účty (`admin/admin`, `verified/verified`, `unverified/unverified`) s tlačítky pro vyplnění.

| Účet         | Po přihlášení                                              |
|--------------|------------------------------------------------------------|
| `admin`      | Dashboard s aktuálním stavem relé + ruční Zapnout / Vypnout |
| `verified`   | Relé se okamžitě zapne, stránka ukáže „Automat BĚŽÍ"        |
| `unverified` | Ověřovací obrazovka (mock) → po kliku na „Ověřit" se chová jako verified |

Odhlášení (`Odhlásit`) na všech kartách volá hub `/off` pro navázaný RPi, takže po skončení sezení automat zůstane vypnutý.

Živé ověření, že vše funguje, můžeš sledovat na **hub dashboardu** (`http://varyshop-trafika-vps:8080/`) — karta RPi ukáže `changed_by: hub` po každé akci z shop-mocku (shop zavolá hub, ten zavolá RPi).

---

## Endpointy

| Metoda | Cesta         | Auth          | Popis                                            |
|--------|---------------|---------------|--------------------------------------------------|
| GET    | `/login`      | ne            | Formulář                                         |
| POST   | `/login`      | ne            | Přihlášení (session cookie)                       |
| POST   | `/logout`     | session       | Odhlášení + vypnutí relé                          |
| GET    | `/`                  | session       | Role-based home + viditelný countdown timer      |
| GET    | `/verify`            | session       | Ověřovací obrazovka (jen pro unverified)         |
| POST   | `/verify`            | session       | Označí session jako ověřenou                     |
| POST   | `/relay/on`          | session+verif | Admin nebo verified → hub `/on`                  |
| POST   | `/relay/off`         | session       | Kdokoli → hub `/off`                             |
| POST   | `/session/heartbeat` | session       | Liveness ping (browser žije). Vrací aktuální view timeru. |
| POST   | `/session/extend`    | session       | Posune `expires_at` o `EXTEND_SECONDS` (cap na MAX). |
| POST   | `/session/end`       | -             | Volá `pagehide`/`sendBeacon` při zavření tabu.   |
| GET    | `/api/health`        | ne            | Health check (Docker HEALTHCHECK)                |

---

## Denní provoz

```bash
cd /opt/trafika-shop-mock
docker compose logs -f          # živé logy
docker compose restart          # po editaci .env
docker compose pull && docker compose up -d   # upgrade image
```

---

## Časovač + auto-off

Po loginu uživatel dostane **viditelný countdown** (default 3 min). Když dojde na 0, relé se vypne. Uživatel může čas prodloužit kliknutím na **Prodloužit o X min** — kolik se přidá je konfigurovatelné, ale nikdy se nepřesáhne hard cap `MAX_SESSION_SECONDS` (default 15 min od loginu).

| Mechanismus | Co řeší | Default | Env var |
|---|---|---|---|
| **Countdown timer** | „Kolik času ti zbývá" | 180 s | `SESSION_DURATION_SECONDS` |
| **Extend tlačítko** | Dobrovolné prodloužení | +180 s | `EXTEND_SECONDS` |
| **Liveness heartbeat** | Browser zavřený / crash / sleep | 30 s bez pingu | `HEARTBEAT_TIMEOUT_SECONDS` |
| **Hard cap** | Maximální session | 15 min | `MAX_SESSION_SECONDS` |

Implementace:

- Server drží `_active[sid] = {user_id, last_alive, expires_at, started_at}`.
- **Liveness ping** — `setInterval(ping, 10s)` v JS, server jen bumpuje `last_alive`.
- **Extend** — `POST /session/extend` posune `expires_at` o `EXTEND_SECONDS` (cap na `started_at + MAX_SESSION_SECONDS`). UI immediately restartuje countdown.
- **Reaper** — každé 2 s projde sessions: drop pokud `now - last_alive > HEARTBEAT_TIMEOUT_SECONDS` (reason `disconnected`) **nebo** `now > expires_at` (reason `timer_expired`). Při expiraci poslední session se zavolá hub `/off`.
- **Pagehide beacon** — `navigator.sendBeacon('/session/end')` při zavření tabu pro okamžitý cleanup.
- **Multi-session / multi-tab** — relé padá až když `len(_active) == 0`. Každá session má vlastní časovač.

Pozn.: dokud nemáme reálný GPIO signál z vending automatu (purchase / button press), tohle je čisté UI-side řízení. Až bude HW signál (nákup proběhl), můžeme buď automaticky prodloužit timer, nebo ukončit session ihned po dokončení nákupu — to už je business decision.

## Co to simuluje / co zatím neřeší

- **Real Odoo integrace** — tento modul má stejné chování, jaké bude mít server action v Odoo: po úspěšném loginu volat hub `/api/rpi/<host>/on`, po logoutu (nebo při ztrátě session) `/off`.
- **Mapování user → RPi** — mock řídí jeden pevně nastavený RPi (`RPI_HOSTNAME`). V reálu by trafika Odoo účet měla ve svém profilu uvedený RPi hostname (nebo by trafika měla dedikované Odoo).
- **Věková/identity verifikace** — v `VERIFY_HTML` je jen tlačítko „Ověřit (MOCK)". V reálu bankovní identita / OP / OAuth apod.
- **Persistence sessions po restartu shop-mocku** — `_active` je in-memory. Po `docker compose restart` se mapa ztratí, aktivní browser obdrží HTTP 410 na nejbližší heartbeat a je přesměrován na /login. Relé se vypne (reaper při pádu shop-mocku neoff, ale Odoo/hub integrace by měla mít nezávislý timeout watchdog na hubu — TODO pro future).

---

## Changelog

- **2026-04-18** — Nahrazena activity-based idle detekce **viditelným countdown timerem** + tlačítkem Prodloužit. Defaultně 3 min od loginu, +3 min za click, max 15 min. Activity tracking (mouse / key / scroll listener) odstraněn — uživatel teď čas řídí explicitně. Liveness heartbeat zachovaný pro disconnect detection. Nové env vars `SESSION_DURATION_SECONDS` a `EXTEND_SECONDS`; `ACTIVITY_TIMEOUT_SECONDS` zrušen.
- **2026-04-18** — Activity-based idle timeout. Heartbeat se rozpadl na liveness (auto, každých 10 s, timeout 30 s) a activity (debounced user interakce, timeout 180 s = 3 min). Reaper kontroluje obě osy + hard cap MAX_SESSION_SECONDS. Nový env var `ACTIVITY_TIMEOUT_SECONDS`.
- **2026-04-18** — Presence tracking a auto-off. JS na home page pingá `/session/heartbeat` každých 10 s, `pagehide` spouští `sendBeacon` na `/session/end`. Server reaper (každých 5 s) pročistí sessions nad `HEARTBEAT_TIMEOUT_SECONDS` nebo `MAX_SESSION_SECONDS`. Relé se vypne až když žádná session nezůstane aktivní (multi-tab / multi-user safe).
- **2026-04-18** — Počáteční verze mock shopu. 3 hardcoded účty, role-based flow (admin manual / verified auto / unverified → verify → auto), network_mode: host, volání hubu na 127.0.0.1:8080.
