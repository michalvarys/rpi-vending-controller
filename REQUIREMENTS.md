# Trafika vending controller — funkční požadavky

Souhrn všech funkčních požadavků, jak postupně vznikaly v průběhu vývoje.
Slouží jako spec pro další iterace (zejména implementaci reálného Odoo modulu)
a jako kontrolní list, co všechno systém musí umět.

---

## 1. Cíl projektu

Bezpečně řídit napájení vending automatu v trafice. Automat **smí být pod proudem
jen když to autorizuje server** — typicky po přihlášení zákazníka přes shop
(reálně Odoo, mockované přes shop-mock). Více trafik se centrálně monitoruje.

## 2. Architektura (komponenty)

| Komponenta | Kde běží | Repo adresář | Role |
|---|---|---|---|
| **Vending controller** | Raspberry Pi v každé trafice | `rpi/` | Řídí relé na napájení automatu, displeje QR kód, hlásí stav |
| **Central hub** | VPS | `hub/` | Agreguje stav všech RPi, dovoluje admin manuální zásah, validuje QR tokeny |
| **Shop (mock)** | VPS, port 8081 | `shop-mock/` | Suplující Odoo — login + autorizace zákazníka, řídí spuštění relé přes hub |
| **Reálné Odoo** | Veřejný server (TODO) | mimo repo | Produkční nástupce shop-mocku, musí implementovat stejné endpointy |

Síťování: **Tailscale** mezi RPi a VPS (RPi za NAT, VPS s veřejnou IP, ale shop pro
zákazníky musí nakonec běžet na **veřejné doméně**).

## 3. Bezpečnostní invarianty (NIKDY neporušit)

1. Po startu kontejneru / RPi začíná relé **VŽDY na OFF**. Žádná persistence „bylo zapnuto, znovu zapnu".
2. Bez **autorizovaného signálu od shopu** se relé nesmí zapnout.
3. Bez **QR-tokenu z konkrétního RPi** shop nesmí vědět, který automat aktivovat — žádný „default RPi" pro zákaznický login.
4. **QR token musí rotovat** (default 60 s), aby se nedal sdílet ani replayovat.
5. **Liveness + timer + hard cap** — relé se vypne sám při kterékoli z těchto podmínek (viz §6).

---

## 4. Vending controller (rpi/)

### 4.1 Webhook příjem

- `POST /webhook/on` (token-protected) — externí signál z Odoo / hubu, sepne relé.
- `POST /webhook/off` (token-protected) — externí signál vypnout.
- Token = `WEBHOOK_TOKEN` env proměnná, každé RPi má vlastní.

### 4.2 Vlastní dashboard

- `GET /` — HTML s aktuálním stavem relé, časem poslední změny, log poslední ~100 událostí.
- `POST /ui/toggle` — manuální přepnutí pro lokální testování.
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
- Token = HMAC-SHA256(`WEBHOOK_TOKEN`, `hostname:floor(now/QR_ROTATE_SECONDS)`)[:9 bytů, base64url].
- URL formát: `{QR_BASE_URL}/activate/{hostname}/{token}`.
- Default rotace **60 s**, akceptace 1 předchozí window pro drift mezi scanem a loginem.
- QR knihovna **bundlovaná v Docker imagi** (ne CDN) — funguje i v offline prohlížeči.

### 4.5 Reálné GPIO (TODO, zatím mock)

- `set_relay()` v `rpi/app.py` je mock funkce — bude nahrazena `gpiozero.OutputDevice(pin, active_high=..., initial_value=False)`.
- Compose musí povolit přístup k `/dev/gpiomem` (`devices: ["/dev/gpiomem:/dev/gpiomem"]`, `group_add: [gpio]`) — řádky jsou v compose okomentované, odkomentují se až bude potvrzený pin.

### 4.6 Vzdálený restart

- `POST /api/restart` (token-protected) — provede **host reboot** (ne jen kontejner) přes `reboot(2)` syscall + `CAP_SYS_BOOT`.
- Pokud chybí cap, fallback na `os._exit(0)` (restart kontejneru) + log `host_reboot_failed`.
- Hub má proxy `POST /api/rpi/<host>/restart` → tlačítko `↻` v dashboardu s confirm dialogem.

### 4.7 Kiosk mód (volitelný)

- Skript `scripts/install-kiosk.sh` na RPi s displejem nastaví:
  - Apt: chromium, unclutter
  - XDG autostart: po desktop loginu spustí `~/.local/bin/trafika-kiosk.sh`
  - Wrapper čeká na `/api/health`, pak `chromium --kiosk http://localhost:8080/qr`
  - `raspi-config nonint do_boot_behaviour B4` — desktop autologin

---

## 5. Central hub (hub/)

### 5.1 Registry RPi

- Editovatelný `rpis.yml` (bind-mount z hosta, ne v imagi) se seznamem `{hostname, token, display_name, port}`.
- Restart hubu po změně, žádné hot-reload.

### 5.2 Agregace stavu

- Background poller, každé 3 s pro každý RPi paralelně: `/api/state`, `/api/logs`, `/api/status`, `/api/device`.
- Cache udržuje poslední známý `device_info` i když je RPi krátkodobě offline (aby karta nezmizela).

### 5.3 Dashboard

- `GET /` — grid karet, jedna na RPi.
- Karta obsahuje:
  - Display name + hostname + **lokace** + odkaz na lokální RPi dashboard
  - Velký dot ON/OFF/unknown + textový stav
  - **Health badge**: `healthy` / `N issues` (`bez internetu`, `málo místa`, `přehřátí`, `vysoká RAM`) / `offline`
  - Metriky: internet ✓/✗, uptime, disk %, CPU °C, RAM %, load
  - Tlačítka **ON / OFF / Toggle / ↻ Restart** (disabled když offline)
  - Rozbalitelné **„Info o zařízení"** (model, sériové č., HW rev, OS, kernel, host hostname, public IP, RAM total)
  - Rozbalitelný **log** (posledních 20 událostí)
- **Auto-refresh 3 s zachovává otevřené `<details>` a scroll position.**

### 5.4 Proxy ovládání

- `POST /api/rpi/<host>/on|off|toggle|restart` — proxuje na příslušný endpoint RPi s tokenem z `rpis.yml`.

### 5.5 QR validace

- `POST /api/qr/validate` body `{rpi_hostname, token}` → `{valid, window_offset}`.
- Hub si v `rpis.yml` najde RPi, přepočte HMAC pro current + previous window.
- Volá ji shop-mock (a budoucí Odoo).

---

## 6. Shop / autentizace (shop-mock + budoucí Odoo)

### 6.1 Mock účty

| Login | Heslo | Role |
|---|---|---|
| `admin` | `admin` | Operátor / maintenance |
| `verified` | `verified` | Ověřený zákazník |
| `unverified` | `unverified` | Neověřený zákazník (musí projít mock verification step) |

Reálné Odoo bude tyto role mapovat na svůj user model.

### 6.2 Login flow

- **Login je vždy povolený** (i bez QR scanu).
- Po loginu **bez** pinned RPi → `/expired` page „Naskenuj QR u automatu" (relé se nezapne).
- Po loginu **s** pinned RPi → home s aktivací podle role.

### 6.3 QR aktivace

- `GET /activate/<rpi>/<token>` — validuje přes hub `/api/qr/validate`.
- Při validu nastaví `session["pinned_rpi"]` + `session["pinned_at"]` (timestamp), zachová `user_id` a `verified_flag` ze session.
- Redirect na `/login` — pokud uživatel už je přihlášený, projde rovnou na `/`.
- **Customer scénář:** přihlas se jednou, pak už jen scanuj QR různých automatů, žádné re-credentials.

### 6.4 Role-based home

- **Verified zákazník**: pinned_rpi → home zavolá `hub /on`. Stránka „Automat BĚŽÍ".
- **Unverified zákazník**: pinned_rpi → redirect na `/verify` (mock ověření button) → po POST `/verify` se chová jako verified.
- **Admin** s pinned_rpi: home s manuálním Zapnout/Vypnout, žádný countdown timer.
- **Admin** bez pinned_rpi: info page s odkazem na hub dashboard (admin nepoužívá shop pro správu).

### 6.5 `pinned_rpi` TTL

- `pinned_at` má TTL = `MAX_SESSION_SECONDS` (default 15 min).
- Když uživatel přijde se starým cookie (např. další den), pin se zahodí → `/expired` → musí znovu naskenovat QR.

---

## 7. Session lifecycle a auto-off

### 7.1 Tři nezávislé timery (zákaznická session)

| Co | Default | Co řeší |
|---|---|---|
| **Liveness heartbeat** | 30 s bez pingu od JS | Browser zavřený, crash, sleep, ztráta sítě |
| **Countdown timer** | 60 s od přihlášení (extendable +30 s) | „Zákazník má 1 minutu, pak musí prodloužit" |
| **Hard cap** | 15 min od loginu | Maximum bez ohledu na cokoli |

### 7.2 Countdown UI

- Velký `mm:ss` display na home page.
- Tlačítko **„Prodloužit o 30 s"** přidá `EXTEND_SECONDS` (cap na hard max).
- Pod 30 s display zčervená a pulzuje.
- Při expiraci JS redirect na `/expired`, server vypne relé.

### 7.3 Admin výjimka

- Admin session má `expires_at = ∞`, žádný countdown ani Extend tlačítko.
- Liveness pořád platí — zavřený admin tab → relé OFF do 30 s.

### 7.4 Auto-off mechanismus

- Reaper thread každé 2 s prochází `_active` mapu sessions.
- Pri expiraci poslední session: `_schedule_relay_off()` s **3 s grace** — pokud se v tom čase někdo (re)registruje (typicky form-submit navigace), off se zruší.
- `pagehide` event v JS volá `navigator.sendBeacon('/session/end')` pro okamžitý drop bez čištění session cookie (race-safe pro form submits).

### 7.5 Po expiraci timeru

- Relé OFF.
- **Uživatel zůstane přihlášený** (session cookie nemizí).
- Browser vidí 410 z heartbeatu → redirect na `/expired`.
- Pinned_rpi vyresetováno, uživatel musí naskenovat čerstvý QR pro další aktivaci.

---

## 8. Operativa (deploy, install, monitoring)

### 8.1 Image registry

- GHCR: `ghcr.io/michalvarys/trafika-{rpi,hub,shop-mock}:latest`.
- Multiarch (linux/amd64 + linux/arm64) build přes GitHub Actions na push do `main`.

### 8.2 Instalace

- **Nový RPi (headless):** `scripts/install.sh` přes `curl | sudo bash`.
  Nainstaluje Docker + Tailscale, zeptá se na hostname / lokaci / QR_BASE_URL,
  vygeneruje token, uloží `.env`, spustí kontejner, vypíše YAML blok pro `rpis.yml`.
  Idempotentní — re-run zachová token a hostname.
  Neinteraktivní mód přes env vars (`TS_HOSTNAME`, `TAILSCALE_AUTH_KEY`, …).
- **Nový RPi (s displejem):** navíc `scripts/install-kiosk.sh` pro fullscreen `/qr`.
- **VPS hub:** ručně dle `HUB-SETUP.md` (curl compose, edit `rpis.yml`).
- **VPS shop-mock:** ručně dle `SHOP-MOCK-SETUP.md`.

### 8.3 Persistence

- RPi `/opt/trafika-rpi/data/events.log` — JSONL log událostí, bind-mount.
- Hub `/opt/trafika-hub/rpis.yml` — registry RPi, bind-mount.
- Shop-mock — žádná persistence, sessions in-memory.

### 8.4 Vzdálený restart

- Hub UI tlačítko `↻` → host reboot RPi (~30-60 s downtime).

### 8.5 Logy

- `docker compose logs -f` na každé komponentě.
- `events.log` na RPi (JSONL).
- `journalctl` jen pokud user záměrně nasadil systemd verzi (legacy, neaktuální).

---

## 9. Síťování

### 9.1 Tailscale

- Všechny RPi + VPS ve stejném tailnetu (`varyshop.eu`).
- RPi → hostname `rpi-vending`, `rpi-vending-2`, …
- VPS → `varyshop-trafika-vps`.
- Tailscale SSH zapnutý (`tailscale up --ssh`) pro vzdálenou správu bez otevření portu 22.

### 9.2 Komunikace

- Hub → RPi: `http://<hostname>:8080` přes tailnet.
- Shop-mock → hub: `http://127.0.0.1:8080` (network_mode: host na VPS).
- Zákazník (telefon) → shop: **veřejná URL** (Odoo musí být veřejně dostupný); v dev používáme tailnet URL s tím, že tester má Tailscale na telefonu.

---

## 10. Otevřené body / TODO

- [ ] **Reálné GPIO** — pin a typ relé (active-HIGH/LOW) zatím nejsou potvrzené.
- [ ] **Reálné Odoo** — implementovat controller `/trafika/activate/<rpi>/<token>` s identickou logikou jako shop-mock; volat hub `/api/qr/validate` a po loginu hub `/on`.
- [ ] **Veřejný shop** — Odoo na veřejné doméně + TLS; QR_BASE_URL v RPi `.env` přepnout z tailnet URL na produkční.
- [ ] **Rotace logů** — RPi `events.log` roste donekonečna, přidat logrotate.
- [ ] **Produkční WSGI** — Flask dev server je OK na tailnetu, gunicorn by byl čistší.
- [ ] **Hub-side timeout watchdog** — pokud shop-mock spadne během aktivace, relé může zůstat ON; hub by měl mít vlastní watchdog na expiraci sessions.
- [ ] **Mapování user → RPi v Odoo** — v reálu by trafika měla v Odoo profilu uvedený RPi hostname (nebo dedikovaná Odoo instance per trafika).
- [ ] **Skutečná identitní verifikace** — místo mock „Ověřit" tlačítka napojit BankID / OP scan / OAuth.
- [ ] **Single-use QR tokeny** — momentálně lze stejný token opakovat během window. Pokud chceme single-use, hub by musel udržovat seznam použitých tokenů.

---

## Změny (chronologicky shrnuté)

1. **04-18** Tailscale tunel RPi ↔ Odoo VPS.
2. Webhook server na RPi + dashboard + JSON event log.
3. systemd → Docker migrace.
4. Central hub na VPS s grid UI a multi-RPi monitorováním.
5. Install script pro čistý RPi.
6. Reportování device health a HW info (status + device endpoints).
7. Hub UI bug fix: zachování otevřených `<details>` při refresh.
8. Mock shop s 3 účty (admin/verified/unverified) a role-based flow.
9. Vzdálený restart (původně container, pak host reboot s CAP_SYS_BOOT).
10. Auto-off při zavření prohlížeče (presence tracking + sendBeacon).
11. Activity-based idle detection → nahrazeno explicit countdown timerem.
12. Timer 1 min + extend 30 s, admin výjimka.
13. Race fix: form submit + pagehide ne-resetuje login (3 s grace window).
14. QR aktivace: rotující HMAC token, hub validate endpoint, /activate flow v shopu.
15. QR knihovna bundlovaná v imagi (no CDN), klikatelný URL pod QR.
16. Customer aktivace **vyžaduje** QR token (přihlášení samo nestačí).
17. Login je vždy povolený, jen aktivace gated; pinned_rpi má TTL pro stale cookie ochranu; po expiraci timeru session přežije pro snadné re-skenování.
18. Kiosk install script — fullscreen `/qr` po bootu RPi s displejem.
