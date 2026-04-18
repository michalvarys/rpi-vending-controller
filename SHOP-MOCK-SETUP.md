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
| GET    | `/`           | session       | Role-based home (admin / verified / unverified) |
| GET    | `/verify`     | session       | Ověřovací obrazovka (jen pro unverified)         |
| POST   | `/verify`     | session       | Označí session jako ověřenou                     |
| POST   | `/relay/on`   | session+verif | Admin nebo verified → hub `/on`                  |
| POST   | `/relay/off`  | session       | Kdokoli → hub `/off`                             |
| GET    | `/api/health` | ne            | Health check (Docker HEALTHCHECK)                |

---

## Denní provoz

```bash
cd /opt/trafika-shop-mock
docker compose logs -f          # živé logy
docker compose restart          # po editaci .env
docker compose pull && docker compose up -d   # upgrade image
```

---

## Presence tracking a automatické vypnutí

Aby relé nezůstalo viset ON po zavření prohlížeče / uspání zařízení / ztrátě sítě, shop-mock si drží in-memory **mapu aktivních sessions** (`sid → last_ping_timestamp`):

- **Heartbeat** — JS na home page pošle `POST /session/heartbeat` každých 10 s.
- **Beacon na unload** — při zavření tabu (event `pagehide`) se přes `navigator.sendBeacon` volá `POST /session/end`, což session okamžitě ukončí (když browser spolupracuje; mobilní Safari někdy ne).
- **Reaper thread** — každých 5 s pročistí: session bez heartbeatu > `HEARTBEAT_TIMEOUT_SECONDS` (default 30 s) je dropnutá. Session starší než `MAX_SESSION_SECONDS` (default 900 s = 15 min) je taky dropnutá.
- **Multi-session / multi-tab** — relé se vypne **až když spadne poslední aktivní session**. Pokud je přihlášený víc účtů / víc tabů, drží je otevřené libovolný z nich.

Tuning v `.env`:
- `HEARTBEAT_TIMEOUT_SECONDS` — jak dlouho čekat bez pingu, než session dropnout.
- `MAX_SESSION_SECONDS` — hard timeout per session (ochrana proti „visící" relé).

## Co to simuluje / co zatím neřeší

- **Real Odoo integrace** — tento modul má stejné chování, jaké bude mít server action v Odoo: po úspěšném loginu volat hub `/api/rpi/<host>/on`, po logoutu (nebo při ztrátě session) `/off`.
- **Mapování user → RPi** — mock řídí jeden pevně nastavený RPi (`RPI_HOSTNAME`). V reálu by trafika Odoo účet měla ve svém profilu uvedený RPi hostname (nebo by trafika měla dedikované Odoo).
- **Věková/identity verifikace** — v `VERIFY_HTML` je jen tlačítko „Ověřit (MOCK)". V reálu bankovní identita / OP / OAuth apod.
- **Persistence sessions po restartu shop-mocku** — `_active` je in-memory. Po `docker compose restart` se mapa ztratí, aktivní browser obdrží HTTP 410 na nejbližší heartbeat a je přesměrován na /login. Relé se vypne (reaper při pádu shop-mocku neoff, ale Odoo/hub integrace by měla mít nezávislý timeout watchdog na hubu — TODO pro future).

---

## Changelog

- **2026-04-18** — Presence tracking a auto-off. JS na home page pingá `/session/heartbeat` každých 10 s, `pagehide` spouští `sendBeacon` na `/session/end`. Server reaper (každých 5 s) pročistí sessions nad `HEARTBEAT_TIMEOUT_SECONDS` nebo `MAX_SESSION_SECONDS`. Relé se vypne až když žádná session nezůstane aktivní (multi-tab / multi-user safe).
- **2026-04-18** — Počáteční verze mock shopu. 3 hardcoded účty, role-based flow (admin manual / verified auto / unverified → verify → auto), network_mode: host, volání hubu na 127.0.0.1:8080.
