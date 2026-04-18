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

| Účet         | Přihlášení bez QR              | Po přihlášení s QR                                              |
|--------------|--------------------------------|------------------------------------------------------------------|
| `admin`      | Info page (link na hub)        | Dashboard daného RPi + ruční Zapnout / Vypnout                  |
| `verified`   | Login OK, ale „Naskenuj QR" page — **relé se nezapne** | Relé se okamžitě zapne, stránka ukáže „Automat BĚŽÍ"    |
| `unverified` | Login OK, ale „Naskenuj QR" page — **relé se nezapne** | Ověřovací obrazovka → po „Ověřit" se chová jako verified |

Přihlášení je vždy vítáno, ale **aktivace automatu vyžaduje čerstvý QR token**. Díky tomu zákazník nemusí zadávat údaje pokaždé — přihlásí se jednou, pak už jen scanuje QR kódy různých automatů. Pinned_rpi má TTL `MAX_SESSION_SECONDS` (15 min); starý cookie z ranního nákupu automat nezapne večer.

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
| GET    | `/activate/<rpi>/<token>` | ne        | Vstup z QR kódu. Validuje přes hub, pinne RPi + pinned_at do session, redirect na /login. Ponechá user_id + verified_flag → přihlášený uživatel pokračuje plynule. |
| GET    | `/expired`           | session       | Stránka „Naskenuj QR" pro přihlášené zákazníky bez (nebo s expirovaným) pinem. |
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
| **Countdown timer** | „Kolik času ti zbývá" | 60 s | `SESSION_DURATION_SECONDS` |
| **Extend tlačítko** | Dobrovolné prodloužení | +30 s | `EXTEND_SECONDS` |
| **Liveness heartbeat** | Browser zavřený / crash / sleep | 30 s bez pingu | `HEARTBEAT_TIMEOUT_SECONDS` |
| **Hard cap** | Maximální session | 15 min | `MAX_SESSION_SECONDS` |

Implementace:

- Server drží `_active[sid] = {user_id, role, last_alive, expires_at, started_at}`.
- **Admin výjimka:** admin session má `expires_at = ∞` a žádný countdown v UI. Stále platí liveness (tab close = off po 30 s). Admin je očekávaně „fully manual" — relé zůstává ON dokud explicitně nestiskne OFF nebo nezavře browser.
- **Liveness ping** — `setInterval(ping, 10s)` v JS, server jen bumpuje `last_alive`.
- **Extend** — `POST /session/extend` posune `expires_at` o `EXTEND_SECONDS` (cap na `started_at + MAX_SESSION_SECONDS`). UI immediately restartuje countdown.
- **Reaper** — každé 2 s projde sessions: drop pokud `now - last_alive > HEARTBEAT_TIMEOUT_SECONDS` (reason `disconnected`) **nebo** `now > expires_at` (reason `timer_expired`). Při expiraci poslední session se zavolá hub `/off`.
- **Pagehide beacon** — `navigator.sendBeacon('/session/end')` při zavření tabu. Drop presence, **nečistí session cookie** (same-origin form submit taky spouští pagehide).
- **Grace window (`GRACE_SECONDS=3`)** — když poslední session odpadne, hub `/off` se nevolá hned. Jakákoli `_register_presence` v grace intervalu ho zruší. Brání race condition u form submit (pagehide+beacon vs. následující navigace).
- **Multi-session / multi-tab** — relé padá až když `len(_active) == 0`. Každá session má vlastní časovač.

Pozn.: dokud nemáme reálný GPIO signál z vending automatu (purchase / button press), tohle je čisté UI-side řízení. Až bude HW signál (nákup proběhl), můžeme buď automaticky prodloužit timer, nebo ukončit session ihned po dokončení nákupu — to už je business decision.

## QR aktivační flow

Reálný produkční scénář: zákazník stojí u automatu, naskenuje mobilem QR na displeji, otevře se mu shop, přihlásí se, automat naskočí. QR rotuje (default 60 s), takže sdílet URL přes WhatsApp partě kámošů nefunguje — do minuty je neplatný.

Kroky:

1. **RPi** má endpoint `/qr` — live stránka s QR kódem. Token přepočítává každých `QR_ROTATE_SECONDS` vteřin jako `HMAC-SHA256(WEBHOOK_TOKEN, hostname + ":" + floor(now / 60))[:9]` v base64url. QR nese URL `<QR_BASE_URL>/activate/<hostname>/<token>`.
2. **Uživatel** QR scanne, prohlížeč otevře `/activate/rpi-vending/abc123`.
3. **Shop-mock** zavolá `POST <HUB_URL>/api/qr/validate` se stejnými parametry. Hub si v `rpis.yml` najde RPi, zrekonstruuje token, porovná. Akceptuje aktuální window + přechozí (drift mezi zobrazením QR a dokončením loginu).
4. Pokud valid → `session["pinned_rpi"] = hostname`, redirect na `/login`. Pin přežije `session.clear()` při úspěšném loginu (jinak by pin zmizel).
5. Home page volá hub `/on` pro **pinned RPi** (ne pro hardcoded default). Nad badge zobrazí „přes QR" tag.

**Pozn. pro integraci s reálným Odoo:**

Tento `/activate/<rpi>/<token>` routing musí v Odoo modulu přibýt jako vlastní controller (např. `/trafika/activate/<rpi>/<token>`). Logika stejná: volat hub `/api/qr/validate`, pinovat RPi do session, po přihlášení zákazníka na něj zavolat hub `/on`. Hub z shop-mocku i z Odoo volá stejnou HTTP API.

## Co to simuluje / co zatím neřeší

- **Real Odoo integrace** — tento modul má stejné chování, jaké bude mít server action v Odoo: po úspěšném loginu volat hub `/api/rpi/<host>/on`, po logoutu (nebo při ztrátě session) `/off`.
- **Mapování user → RPi** — mock řídí jeden pevně nastavený RPi (`RPI_HOSTNAME`). V reálu by trafika Odoo účet měla ve svém profilu uvedený RPi hostname (nebo by trafika měla dedikované Odoo).
- **Věková/identity verifikace** — v `VERIFY_HTML` je jen tlačítko „Ověřit (MOCK)". V reálu bankovní identita / OP / OAuth apod.
- **Persistence sessions po restartu shop-mocku** — `_active` je in-memory. Po `docker compose restart` se mapa ztratí, aktivní browser obdrží HTTP 410 na nejbližší heartbeat a je přesměrován na /login. Relé se vypne (reaper při pádu shop-mocku neoff, ale Odoo/hub integrace by měla mít nezávislý timeout watchdog na hubu — TODO pro future).

---

## Changelog

- **2026-04-19** — Login teď vždy projde, ale **aktivace automatu** vyžaduje čerstvý QR pin. Zákazník bez QR po přihlášení vidí /expired („Naskenuj QR") místo aktivace. `pinned_rpi` má TTL = MAX_SESSION_SECONDS, po expiraci se resetuje. Session (user_id + verified_flag) přežívá — uživatel se přihlásí jednou, pak už jen scanuje QR různých automatů.
- **2026-04-18** — **Bez QR se nepřihlásíš.** Customer login (verified/unverified) vyžadoval `session["pinned_rpi"]`. Revertováno — přihlášení se nebránil, jen aktivace. (Blokovat login byl příliš přísný UX.)
- **2026-04-18** — QR aktivační flow. `GET /activate/<rpi>/<token>` validuje přes hub, pinne RPi do session, provede uživatele loginem. Po loginu home volá hub `/on` pro pinned RPi místo hardcoded defaultu. Ze shop-mocku `RPI_HOSTNAME` se stal `DEFAULT_RPI_HOSTNAME` — používá se jen pro login bez QR (testovací nebo admin).
- **2026-04-18** — Admin session je bez odpočtu. Admin karta v UI neobsahuje countdown ani Extend tlačítko; `expires_at = ∞`, reaper nikdy neexpiruje admina podle timeru. Liveness timeout (30 s bez heartbeatu) pořád platí.
- **2026-04-18** — Defaulty timeru zkráceny: `SESSION_DURATION_SECONDS=60` (z 180), `EXTEND_SECONDS=30` (z 180). Hard cap zachován (900 s). Vending UX: kratší kus „kreditu" + drobnější extends je realističtější pro krátký nákup.
- **2026-04-18** — Nahrazena activity-based idle detekce **viditelným countdown timerem** + tlačítkem Prodloužit. Defaultně 3 min od loginu, +3 min za click, max 15 min. Activity tracking (mouse / key / scroll listener) odstraněn — uživatel teď čas řídí explicitně. Liveness heartbeat zachovaný pro disconnect detection. Nové env vars `SESSION_DURATION_SECONDS` a `EXTEND_SECONDS`; `ACTIVITY_TIMEOUT_SECONDS` zrušen.
- **2026-04-18** — Activity-based idle timeout. Heartbeat se rozpadl na liveness (auto, každých 10 s, timeout 30 s) a activity (debounced user interakce, timeout 180 s = 3 min). Reaper kontroluje obě osy + hard cap MAX_SESSION_SECONDS. Nový env var `ACTIVITY_TIMEOUT_SECONDS`.
- **2026-04-18** — Presence tracking a auto-off. JS na home page pingá `/session/heartbeat` každých 10 s, `pagehide` spouští `sendBeacon` na `/session/end`. Server reaper (každých 5 s) pročistí sessions nad `HEARTBEAT_TIMEOUT_SECONDS` nebo `MAX_SESSION_SECONDS`. Relé se vypne až když žádná session nezůstane aktivní (multi-tab / multi-user safe).
- **2026-04-18** — Počáteční verze mock shopu. 3 hardcoded účty, role-based flow (admin manual / verified auto / unverified → verify → auto), network_mode: host, volání hubu na 127.0.0.1:8080.
