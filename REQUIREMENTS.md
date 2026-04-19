# Trafika vending controller — funkční požadavky

Souhrn všech funkčních požadavků, jak postupně vznikaly v průběhu vývoje.
Slouží jako spec pro další iterace (zejména implementaci reálného Odoo modulu)
a jako kontrolní list, co všechno systém musí umět.

---

## 1. Cíl projektu

Bezpečně řídit napájení vending automatu v trafice. Automat **smí být pod proudem
jen když to autorizuje server** — po naskenování QR kódu a přihlášení zákazníka
přes shop (reálně Odoo, mockované přes shop-mock). Více trafik se centrálně
monitoruje přes hub.

## 2. Architektura (komponenty)

| Komponenta | Kde běží | Repo adresář | Role |
|---|---|---|---|
| **Vending controller** | Raspberry Pi v každé trafice | `rpi/` | Řídí relé na napájení automatu, displeje QR kód, hlásí stav |
| **Central hub** | VPS | `hub/` | Agreguje stav všech RPi, dovoluje admin manuální zásah, validuje QR tokeny, autentizuje shop pro control calls |
| **Shop (mock)** | VPS, port 8081 | `shop-mock/` | Suplující Odoo — login + autorizace zákazníka, po scanu QR řídí aktivaci relé přes hub |
| **Reálné Odoo** | Veřejný server (TODO) | mimo repo | Produkční nástupce shop-mocku, musí implementovat stejné endpointy |

Síťování: **Tailscale** mezi RPi a VPS (RPi za NAT). Shop (pro zákazníky) je na veřejné doméně s reverse proxy.

**Produkční domény:**
- `https://automaty.elite-trafika.cz` — shop (supluje Odoo)
- `https://hub.elite-trafika.cz` — central hub admin
- `https://login.tailscale.com` — tailnet admin

## 3. Bezpečnostní invarianty (NIKDY neporušit)

1. Po startu kontejneru / RPi začíná relé **VŽDY na OFF**. Žádná persistence „bylo zapnuto, znovu zapnu".
2. Bez **autorizovaného signálu od shopu** se relé nesmí zapnout.
3. Bez **QR-tokenu z konkrétního RPi** shop nesmí vědět, který automat aktivovat — žádný „default RPi" fallback pro zákaznický login.
4. **QR token musí rotovat** (default 60 s) a je **single-use** (hub si pamatuje spotřebované tokeny, druhé použití odmítne).
5. **Liveness + timer + hard cap** — relé se vypne sám při kterékoli z těchto podmínek (viz §7).
6. **Hub na veřejné doméně je za Basic Auth nebo Bearer tokenem.** Žádné control endpointy nesmí být dostupné anonymně.
7. **Browser BACK po expiraci timeru neobnovuje session.** Drop history + `Cache-Control: no-store` brání auto-aktivaci ze staré stránky.

---

## 4. Vending controller (rpi/)

### 4.1 Webhook příjem

- `POST /webhook/on` (token-protected) — externí signál z hubu, sepne relé.
- `POST /webhook/off` (token-protected) — externí signál vypnout.
- Token = `WEBHOOK_TOKEN` env proměnná, každé RPi má vlastní.

### 4.2 Vlastní dashboard

- `GET /` — HTML s aktuálním stavem relé, časem poslední změny, log poslední ~100 událostí.
- `POST /ui/toggle` — manuální přepnutí pro lokální testování (bez tokenu, pro diagnostiku na RPi).
- Auto-refresh každé 2 s.

### 4.3 Reportování stavu pro hub

- `GET /api/state` — relay ON/OFF, čas a zdroj poslední změny, device name.
- `GET /api/status` — uptime, internet ping (1.1.1.1:53), disk free %, CPU temp, RAM, load, **`issues[]`** (`no-internet`, `low-disk`, `high-temp`, `high-memory`).
- `GET /api/device` — model RPi, sériové č., HW revision, host OS, kernel, RAM total, hostname, public IP (cached, refresh 1 h), **`location`** (volitelná z env `LOCATION`).
- `GET /api/logs` — posledních 100 událostí JSONL.
- `GET /api/health` — pro Docker HEALTHCHECK i hub.

### 4.4 QR kód pro zákaznickou aktivaci

- `GET /qr` — fullscreen stránka s **rotujícím** QR kódem + lidsky čitelnou URL pod ním (klikatelný link pro testování ze stejného browseru).
- `GET /api/qr` — vrací `{device, token, url, rotate_at, rotate_seconds}`.
- Token = `base64url(HMAC-SHA256(WEBHOOK_TOKEN, "hostname:floor(now/QR_ROTATE_SECONDS)")[:9 bytů])`.
- URL: `{QR_BASE_URL}/activate/{hostname}/{token}` — `QR_BASE_URL=https://automaty.elite-trafika.cz` v produkci.
- Default rotace **60 s**, akceptace 1 předchozí window pro drift mezi scanem a loginem.
- **Single-use:** hub si zapamatuje (rpi, token) pár; druhé použití vrací `valid=false, reason="token already used"`.
- QR knihovna (`davidshimjs/qrcodejs`) **bundlovaná v Docker imagi** — offline provoz.

### 4.5 Reálné GPIO (TODO, zatím mock)

- `set_relay()` v `rpi/app.py` je mock funkce — bude nahrazena `gpiozero.OutputDevice(pin, active_high=..., initial_value=False)`.
- Compose musí povolit přístup k `/dev/gpiomem` (`devices: ["/dev/gpiomem:/dev/gpiomem"]`, `group_add: [gpio]`) — řádky jsou v compose okomentované, odkomentují se až bude potvrzený pin.

### 4.6 Vzdálený restart

- `POST /api/restart` (token-protected) — provede **host reboot** (ne jen kontejner) přes `reboot(2)` syscall + `CAP_SYS_BOOT`.
- Pokud chybí cap, fallback na `os._exit(0)` (restart kontejneru) + log `host_reboot_failed`.
- Hub má proxy `POST /api/rpi/<host>/restart` → tlačítko `↻` v dashboardu s confirm dialogem.

### 4.7 Kiosk mód (volitelný)

- Skript `scripts/install-kiosk.sh` na RPi s displejem:
  - Apt: chromium, unclutter
  - XDG autostart: `~/.local/bin/trafika-kiosk.sh` po desktop loginu
  - Wrapper čeká na `/api/health`, pak `chromium --kiosk http://localhost:8080/qr`
  - `raspi-config nonint do_boot_behaviour B4` — desktop autologin

---

## 5. Central hub (hub/)

### 5.1 Registry RPi

- Editovatelný `rpis.yml` (bind-mount z hosta, ne v imagi) se seznamem `{hostname, token, display_name, port}`.
- Restart hubu po změně.

### 5.2 Autentizace

- **Basic Auth** (`HUB_ADMIN_USER` / `HUB_ADMIN_PASSWORD`) pro admin dashboard a control endpointy — prohlížeč zobrazí native prompt.
- **Bearer token** (`HUB_API_TOKEN`) pro server-to-server volání ze shopu / Odoo. Decoupled od admin hesla.
- Bypass: `GET /api/health` (Docker healthcheck) a `POST /api/qr/validate` (HMAC validace je sama o sobě dostatečná).
- Bez nastaveného hesla / tokenu hub běží OPEN — startup log vypíše WARNING.

### 5.3 Agregace stavu

- Background poller, každé 3 s pro každý RPi paralelně: `/api/state`, `/api/logs`, `/api/status`, `/api/device`.
- Cache udržuje poslední známý `device_info` i když je RPi krátkodobě offline.

### 5.4 Dashboard

- `GET /` (Basic Auth) — grid karet, jedna na RPi.
- Karta obsahuje:
  - Display name + hostname + **lokace** + odkaz na lokální RPi dashboard
  - Velký dot ON/OFF/unknown + textový stav
  - **Health badge**: `healthy` / `N issues` / `offline`
  - Metriky: internet ✓/✗, uptime, disk %, CPU °C, RAM %, load
  - Tlačítka **ON / OFF / Toggle / ↻ Restart** (disabled když offline)
  - Rozbalitelné **„Info o zařízení"** (model, sériové č., HW rev, OS, kernel, host hostname, public IP, RAM total)
  - Rozbalitelný **log** (posledních 20 událostí)
- Auto-refresh 3 s zachovává otevřené `<details>` a scroll position.

### 5.5 Proxy ovládání

- `POST /api/rpi/<host>/on|off|toggle|restart` (auth required) — proxuje na příslušný endpoint RPi s tokenem z `rpis.yml`.

### 5.6 QR validace

- `POST /api/qr/validate` (bypass auth) body `{rpi_hostname, token}` → `{valid, window_offset}`.
- Hub najde RPi v `rpis.yml`, přepočte HMAC pro current + previous window.
- Single-use enforcement: po úspěšné validaci uloží `(rpi, token)` do in-memory mapy; druhá validace téhož páru vrátí `valid=false, reason="token already used"`. Housekeeping maže záznamy > 4× rotation window staré.

### 5.7 Produkční deploy (Hetzner quirk)

Hetzner reverse proxy nemá dostupný port 8080 směrem k VPS (anti-abuse). Compose v produkci mapuje `hub` i na **port 8090** (kromě defaultního 8080) — `hub.elite-trafika.cz` upstream míří na 8090, tailnet zůstává přes 8080.

---

## 6. Shop / autentizace (shop-mock + budoucí Odoo)

### 6.1 Mock účty

| Login | Heslo | Role |
|---|---|---|
| `admin` | `admin` | Operátor / maintenance |
| `verified` | `verified` | Ověřený zákazník |
| `unverified` | `unverified` | Neověřený zákazník (musí projít mock verification step) |

Reálné Odoo bude role mapovat na svůj user model.

### 6.2 Login flow

- **Login je vždy povolený** (i bez QR scanu) — shop rozlišuje autentizaci a aktivaci.
- Po loginu **bez** pinned RPi → `/expired` page „Naskenuj QR u automatu" (relé se nezapne).
- Po loginu **s** pinned RPi → home s aktivací podle role.

### 6.3 QR aktivace

- `GET /activate/<rpi>/<token>` — validuje přes hub `/api/qr/validate` (HMAC + single-use).
- Při validu nastaví `session["pinned_rpi"]` + `session["pinned_at"]` (timestamp), zachová `user_id` a `verified_flag` ze session.
- Redirect na `/login` — pokud uživatel už je přihlášený, projde rovnou na `/`.
- **Zákaznický scénář:** přihlas se jednou, pak už jen scanuj QR různých automatů, žádné re-credentials.

### 6.4 Role-based home

- **Verified zákazník**: pinned_rpi → home zavolá `hub /on`. Stránka „Automat BĚŽÍ" + countdown.
- **Unverified zákazník**: pinned_rpi → redirect na `/verify` (mock ověření) → po POST `/verify` se chová jako verified.
- **Admin** s pinned_rpi: home s manuálním Zapnout/Vypnout, **bez countdown timeru** (admin má `expires_at = ∞`, jen liveness platí).
- **Admin** bez pinned_rpi: info page s odkazem na hub dashboard.

### 6.5 `pinned_rpi` + drop history

- `pinned_at` má TTL = `MAX_SESSION_SECONDS` (default 15 min).
- `_drop_history[sid]` sleduje kdy byla session evictována (reaper / beacon). V `home()`: pokud `sid` není v `_active` a byl evictován > `GRACE_SECONDS + 2` s zpět → `pinned_rpi` zrušen, redirect `/expired`.
- Krátké drops (form submit + pagehide, admin Zapnout) uvnitř grace window → povoleno re-register.
- `GET /` posílá `Cache-Control: no-store` — browser BACK vynutí re-fetch, projde server kontrolou.

### 6.6 Server-to-server auth

- Shop drží `HUB_API_TOKEN` a posílá ho v `Authorization: Bearer …` na každé `hub_post` / `hub_state` volání.
- Bez tohoto tokenu shop dostane 401 a relé se nespustí, i když všechno ostatní prošlo.

---

## 7. Session lifecycle a auto-off

### 7.1 Tři nezávislé timery (zákaznická session)

| Co | Default | Co řeší | Env |
|---|---|---|---|
| **Liveness heartbeat** | 30 s bez pingu od JS | Browser zavřený, crash, sleep, ztráta sítě | `HEARTBEAT_TIMEOUT_SECONDS` |
| **Countdown timer** | 60 s od přihlášení | „Zákazník má 1 minutu, pak musí prodloužit" | `SESSION_DURATION_SECONDS` |
| **Extend click** | +30 s | Uživatel si dobrovolně přidá | `EXTEND_SECONDS` |
| **Hard cap** | 15 min od loginu | Maximum bez ohledu na cokoli | `MAX_SESSION_SECONDS` |

### 7.2 Countdown UI (wall-clock)

- Server posílá **absolutní** `expires_at` + `max_expires_at` (epoch seconds).
- JS každý tick přepočte `remaining = max(0, expires_at - Date.now()/1000)`.
- **Sleep/wake správný:** po probuzení zařízení se countdown **okamžitě srovná** (nezamrzne tam, kde usnul JS).
- Tlačítko **„Prodloužit o 30 s"** přidá `EXTEND_SECONDS` (cap na hard max).
- Pod 30 s display zčervená a pulzuje.
- Při expiraci JS redirect na `/expired`, server vypne relé.

### 7.3 Admin výjimka

- Admin session má `expires_at = ∞`, žádný countdown ani Extend tlačítko.
- Liveness pořád platí — zavřený admin tab → relé OFF do 30 s.

### 7.4 Auto-off mechanismus (per-RPi)

- `_active[sid]` zaznamenává `rpi_hostname` pro každou session.
- Reaper thread každé 2 s prochází sessions. Evicce sbírá „affected RPis".
- Pri expiraci **všech sessions pro konkrétní RPi**: `_schedule_relay_off(rpi_hostname)` s **3 s grace**.
- V grace se může někdo re-registrovat (typicky form-submit navigace) → `_cancel_pending_off(rpi_hostname)`.
- Fire callback volá `hub_post("/off", rpi_hostname=rpi)` **explicitně s hostname** — nevoláno v request contextu (Flask session nedostupná).
- `pagehide` event v JS volá `navigator.sendBeacon('/session/end')` pro okamžitý drop bez čištění session cookie.

### 7.5 Po expiraci timeru

- Relé OFF (po grace).
- **Uživatel zůstane přihlášený** (session cookie nemizí).
- Browser vidí 410 z heartbeatu → redirect na `/expired`.
- `pinned_rpi` + `pinned_at` vyresetováno, uživatel musí naskenovat čerstvý QR pro další aktivaci.
- Browser **BACK** → projde serverem, detekce „session dropnuta > grace" → redirect `/expired` (žádná auto-activation).

---

## 8. Operativa (deploy, install, monitoring)

### 8.1 Image registry

- GHCR: `ghcr.io/michalvarys/trafika-{rpi,hub,shop-mock}:latest`.
- Multiarch (linux/amd64 + linux/arm64) build přes GitHub Actions na push do `main`.

### 8.2 Instalace

- **Nový RPi (headless):** `scripts/install.sh` přes `curl | sudo bash`. Nainstaluje Docker + Tailscale, zeptá se na hostname / lokaci / `QR_BASE_URL`, vygeneruje token, uloží `.env`, spustí kontejner, vypíše YAML blok pro `rpis.yml`. Idempotentní. Neinteraktivní mód přes env vars (`TS_HOSTNAME`, `TAILSCALE_AUTH_KEY`, …).
- **Nový RPi (s displejem):** navíc `scripts/install-kiosk.sh` pro fullscreen `/qr` po bootu.
- **VPS hub:** dle `HUB-SETUP.md` (curl compose, edit `rpis.yml`, nastav `HUB_ADMIN_PASSWORD` a `HUB_API_TOKEN`).
- **VPS shop-mock:** dle `SHOP-MOCK-SETUP.md` (stejný `HUB_API_TOKEN` jako hub, veřejná `HUB_PUBLIC_URL` pro admin link).

### 8.3 Persistence

- RPi `/opt/trafika-rpi/data/events.log` — JSONL log událostí, bind-mount.
- Hub `/opt/trafika-hub/rpis.yml` — registry RPi, bind-mount.
- Shop-mock — žádná persistence, sessions in-memory (restart = všichni aktivní zákazníci dostanou 410 → login).

### 8.4 Vzdálený restart

- Hub UI tlačítko `↻` → host reboot RPi (~30-60 s downtime).

### 8.5 Logy

- `docker compose logs -f` na každé komponentě.
- `events.log` na RPi (JSONL).

---

## 9. Síťování

### 9.1 Tailscale (interní)

- Všechny RPi + VPS ve stejném tailnetu (`varyshop.eu`).
- RPi → hostname `rpi-vending`, `rpi-vending-2`, …
- VPS → `varyshop-trafika-vps`.
- Tailscale SSH zapnutý (`tailscale up --ssh`) pro vzdálenou správu.

### 9.2 Veřejné domény (produkce)

- `automaty.elite-trafika.cz` — nginx proxy → VPS:8081 (shop-mock).
- `hub.elite-trafika.cz` — nginx proxy → VPS:**8090** (hub) — port 8080 je u hostingu anti-abuse blokovaný.

### 9.3 Komunikace

- Hub → RPi: `http://<hostname>:8080` přes tailnet (bez auth, tailnet = perimeter).
- Shop → hub: `http://127.0.0.1:8080` (shop-mock s `network_mode: host` na VPS) s `Authorization: Bearer <HUB_API_TOKEN>`.
- Zákazník (telefon) → shop: `https://automaty.elite-trafika.cz` (veřejná doména + TLS).
- Admin → hub: `https://hub.elite-trafika.cz` s Basic Auth.

---

## 10. Otevřené body / TODO

- [ ] **Reálné GPIO** — pin a typ relé (active-HIGH/LOW) zatím nejsou potvrzené.
- [ ] **Reálné Odoo** — implementovat controller `/trafika/activate/<rpi>/<token>` s identickou logikou jako shop-mock; volat hub `/api/qr/validate` a po loginu hub `/on`. Hub API je jazykově-neutrální, Odoo volá stejné HTTP endpointy. Musí posílat `Authorization: Bearer <HUB_API_TOKEN>`.
- [ ] **Veřejný shop Odoo** — produkční Odoo na veřejné doméně místo shop-mocku; `QR_BASE_URL` v RPi `.env` ukáže na něj.
- [ ] **Rotace logů** — RPi `events.log` roste donekonečna, přidat logrotate.
- [ ] **Produkční WSGI** — Flask dev server je OK na tailnetu, gunicorn by byl čistší (týká se hubu + shop-mocku + rpi).
- [ ] **Hub-side timeout watchdog** — pokud shop-mock/Odoo spadne během aktivace, relé může zůstat ON; hub by měl mít vlastní watchdog na expiraci sessions (nyní závisí na shop-mock reaperu).
- [ ] **Mapování user → RPi v Odoo** — v reálu by Odoo user profile měl uvedenou trafiku / RPi hostname; alternativně dedikovaná Odoo instance per trafika.
- [ ] **Skutečná identitní verifikace** — místo mock „Ověřit" tlačítka napojit BankID / OP scan / OAuth / verified.cz.
- [ ] **Single-use token persistence** — aktuálně `_consumed_tokens` žije in-memory, restart hubu ho zapomene. Pro větší spolehlivost přesunout do SQLite / Redis.
- [ ] **Per-zákazník log** v hubu — kdo kdy aktivoval jaký automat (audit trail pro operátora).

---

## Changelog (chronologicky)

1. **04-18** Tailscale tunel RPi ↔ Odoo VPS.
2. Webhook server na RPi + dashboard + JSON event log.
3. systemd → Docker migrace.
4. Central hub na VPS s grid UI a multi-RPi monitorováním.
5. Install script pro čistý RPi.
6. Reportování device health a HW info (status + device endpoints).
7. Hub UI bug fix: zachování otevřených `<details>` při refresh.
8. Mock shop s 3 účty (admin/verified/unverified) a role-based flow.
9. Vzdálený restart (původně container, pak host reboot s `CAP_SYS_BOOT`).
10. Auto-off při zavření prohlížeče (presence tracking + sendBeacon).
11. Activity-based idle detection → nahrazeno explicit countdown timerem.
12. Timer 1 min + extend 30 s, admin výjimka.
13. Race fix: form submit + pagehide nereststuje login (3 s grace window).
14. QR aktivace: rotující HMAC token, hub validate endpoint, /activate flow v shopu.
15. QR knihovna bundlovaná v imagi (no CDN), klikatelný URL pod QR.
16. Customer aktivace **vyžaduje** QR token (přihlášení samo nestačí).
17. Login je vždy povolený, jen aktivace gated; pinned_rpi má TTL; po expiraci timeru session přežije pro snadné re-skenování.
18. Kiosk install script — fullscreen `/qr` po bootu RPi s displejem.
19. **04-19** Veřejné domény `automaty.elite-trafika.cz` + `hub.elite-trafika.cz` (druhá přes port 8090 kvůli Hetzner block).
20. HUB_PUBLIC_URL oddělený od internal HUB_URL (admin link v UI).
21. Hub Basic Auth (admin) + Bearer token (services); shop-mock posílá Bearer.
22. **Per-RPi auto-off:** session zaznamená rpi_hostname, off je explicitní per-host; fix Timer-thread session crash.
23. **BACK po timer expire** → /expired (drop_history + Cache-Control: no-store).
24. **Single-use QR token** — hub trackuje consumed (rpi, token) pairs.
25. **Wall-clock countdown** — server posílá absolutní expires_at, JS přepočítá proti Date.now(); správné po device sleep/wake.
