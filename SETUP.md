# Trafika vending controller — nasazení nového RPi

Tento dokument popisuje, jak od nuly rozjet Raspberry Pi s containerizovaným vending controllerem. Image se stahuje z **GHCR** (`ghcr.io/michalvarys/trafika-rpi:latest`, multiarch) a konfiguruje se přes `.env`. Udržuj tento návod aktuální — po každé změně architektury (nový endpoint, změna portu, reálné GPIO, migrace na jiný runtime) sem doplň postup a přidej záznam do Changelogu.

> **Legacy:** starší apt+systemd instalace (před Docker migrací) je popsána v gitu v commitu před touto změnou. Pokud najdeš na nějakém RPi běžící `trafika-vending.service` přes systemd, před nasazením Dockeru ho zastav a odstraň (viz sekce „Migrace z systemd").

---

## 📺 Volitelně: kiosk režim (fullscreen /qr po bootu)

Pokud má RPi připojený displej (ten, na kterém má zákazník vidět QR), druhý skript nastaví automatický fullscreen Chromium na `http://localhost:8080/qr` po startu — bez přihlašování, bez panelů, bez kurzoru.

```bash
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/scripts/install-kiosk.sh -o install-kiosk.sh
bash install-kiosk.sh
sudo reboot
```

Vyžaduje Raspberry Pi OS **Desktop** (labwc/LXDE). Skript nainstaluje `chromium` + `unclutter`, vygeneruje wrapper (čeká na `/api/health`, pak otevře Chromium), uloží XDG autostart entry a zapne desktop autologin přes `raspi-config`. Spouští se **jako běžný uživatel** (ne root).

**Ruční vypnutí kiosku:** `rm ~/.config/autostart/trafika-kiosk.desktop && sudo reboot`.

---

## 🚀 Rychlá instalace (install script)

Pro čistý Raspberry Pi — jeden příkaz udělá všechno (apt, Docker, Tailscale, compose, start):

```bash
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/scripts/install.sh | sudo bash
```

Nebo bezpečněji (stáhnout, mrknout na kód, spustit):

```bash
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/scripts/install.sh -o install.sh
sudo bash install.sh
```

Script je **interaktivní** — zeptá se na Tailscale hostname a provede tě Tailscale přihlášením (URL v terminálu). **Idempotentní** — když ho pustíš znovu (např. po update), nic nerozbije: zachová existující token a hostname.

**Neinteraktivní režim** (pro automatizaci / multi-device deploy):

```bash
curl -fsSL https://tailscale.com/admin/settings/keys   # vygeneruj auth key v Tailscale admin konzoli
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/scripts/install.sh \
  | sudo TS_HOSTNAME=rpi-trafika-praha \
         DISPLAY_NAME="Trafika Praha" \
         TAILSCALE_AUTH_KEY=tskey-auth-XXXX \
         bash
```

Na konci vypíše YAML blok připravený k vložení do `/opt/trafika-hub/rpis.yml` na VPS. Pak stačí na VPS `cd /opt/trafika-hub && docker compose restart` a nový RPi naskočí do gridu.

**Pokud preferuješ krok-za-krokem pochopení**, pokračuj sekcemi 0–9 níž.

---

## 0. Prerekvizity

- Raspberry Pi s Raspberry Pi OS (Debian) — testováno na Pi s aarch64 (kernel 6.12). Image je multiarch, takže poběží i na amd64 dev stroji.
- SSH přístup, uživatel se sudo právy (v návodu `varyshop`).
- Účet na Tailscale (stejný tenant — `varyshop.eu`), ať se zařízení vidí s Odoo VPS a hubem.
- Relé modul **SB Components Zero-Relay 2-ch** (nebo pin-kompatibilní, active-HIGH).

---

## 0b. eObčanka čtečka (volitelné)

**Deska:** AXAGON CRE-SM3TC (USB-C, kontaktní PC/SC čtečka). Stejný postup platí pro libovolnou CCID čtečku.

**Flow:** zákazník vloží občanku → `pyscard` monitor detekuje insert → SELECT card-management AID `D2 03 10 01 00 01 00 02 02` → pokud SW=9000, karta je potvrzená eObčanka → relé ON na `RELAY_ON_SECONDS` (default 60 s), pak auto-off. Kiosk `/qr` stránka zobrazí zelený overlay s odpočtem.

**Nečteme DOB z čipu** — novější eObčanky (2021+) mají ID certifikát za PACE/secure-messaging, což implementovat by byla 1-2 týdny kryptografické práce. Kontaktní čtečka zde funguje jen jako "karta vložena" trigger. Pro právní ověření věku je správná cesta eDoklady/Bank iD přes existující QR flow.

**Prerekvizity na hostu (řeší `install.sh`):**
```bash
sudo apt install -y pcscd libccid pcsc-tools
sudo systemctl enable --now pcscd.socket
pcsc_scan -n   # musí ukázat "Generic Smart Card Reader Interface"
```

**Kontejner** — `docker-compose.yml` bind-mountuje `/run/pcscd` → kontejner talks k host pcscd přes socket. Žádný USB passthrough, žádný privileged.

**Env proměnné v `.env`:**
- `CARD_READER_ENABLED=auto` (`auto` = zapnout pokud pyscard + čtečka dostupné; `false` = úplně vypnout; `true` = vynutit, chyba = issue flag)
- `RELAY_ON_SECONDS=60` — jak dlouho relé zůstane ON po card insertu / webhooku

**Verifikace:** po vložení karty `curl http://127.0.0.1:8080/api/card/state` musí vrátit `is_eobcanka: true`, a v `data/events.log` `relay_on source=card`.

### Diagnostika / debugging

**Host — pcscd vidí čtečku?**
```bash
systemctl status pcscd.socket         # musí být active (listening)
pcsc_scan -n                          # enumerace + ATR každé změny stavu; Ctrl+C ukončí
                                      # Bez karty: "No card inserted". S kartou: ATR.
```
Typický ATR eObčanky: `3B 7E 94 00 00 80 25 D2 03 10 01 00 56 00 00 00 02 02 00` (sekvence `D2 03 10 01 00` v historických bytech je fingerprint). ATR se ale liší podle šarže čipu — **na ATR se nespoléhej**, správný fingerprint je SW=9000 na SELECT card-mgmt AID.

**Kontejner — vidí čtečku?**
```bash
docker compose exec trafika-rpi ls /run/pcscd/                       # musí obsahovat pcscd.comm socket
docker compose exec trafika-rpi python -c "from smartcard.System import readers; print(readers())"
# očekávané: [<Generic Smart Card Reader Interface ...>]
```

**App state — vložení, AID check, relé:**
```bash
curl -s http://127.0.0.1:8080/api/card/state | python3 -m json.tool
# {
#   "reader_present": true,     ← pcscd vidí čtečku
#   "card_present": true,       ← karta je zasunutá
#   "is_eobcanka": true,        ← SELECT card-mgmt AID vrátil 9000
#   "last_event_at": "2026-04-21T12:23:38",
#   "error": null,              ← nenull pokud AID check selhal
#   "enabled": true             ← CARD_READER_ENABLED != false
# }

curl -s http://127.0.0.1:8080/api/state | python3 -m json.tool
# relay, changed_at, changed_by, relay_expires_at (null = paused nebo OFF)
```

**Live sledování eventů** (sleduj insert/remove + relay_on/off):
```bash
tail -f /opt/trafika-rpi/data/events.log | grep -E "relay|card"
docker compose logs -f trafika-rpi 2>&1 | grep -iE "card|reader"
```

**Surové APDU zkoušky** (když chceš vidět raw komunikaci s kartou — např. při porušené detekci nebo experimentech s PACE):
```bash
docker compose exec trafika-rpi python - <<'PY'
from smartcard.System import readers
c = readers()[0].createConnection()
c.connect()
def tx(apdu, label):
    data, sw1, sw2 = c.transmit(apdu)
    hx = ' '.join(f'{b:02X}' for b in apdu)
    dhx = ' '.join(f'{b:02X}' for b in data) if data else '-'
    print(f'[{label}] >> {hx}\n[{label}] << {dhx}  SW={sw1:02X}{sw2:02X}')
# SELECT card-management AID (eObčanka fingerprint)
tx([0x00,0xA4,0x04,0x0C,0x09, 0xD2,0x03,0x10,0x01,0x00,0x01,0x00,0x02,0x02], 'SEL_CARD_MGMT')
# SELECT file-management AID + SELECT EF 0x0001 (ID cert)
tx([0x00,0xA4,0x04,0x0C,0x0A, 0xD2,0x03,0x10,0x01,0x00,0x01,0x03,0x02,0x01,0x00], 'SEL_FILE_MGMT')
_, sw1, sw2 = tx([0x00,0xA4,0x08,0x00,0x02, 0x00,0x01], 'SEL_EF')
# T=0: pokud SW1=61 následuje GET RESPONSE
if sw1 == 0x61:
    tx([0x00,0xC0,0x00,0x00,sw2], 'GET_RESP')
# READ BINARY cert — selže s 6982 (security) na 2021+ kartách, to je OK
tx([0x00,0xB0,0x00,0x00,0xD0], 'READ_BIN')
PY
```

**Co znamenají časté SW kódy:**
- `9000` = OK
- `61XX` = `XX` bytů odpovědi čeká, udělej `00 C0 00 00 XX` GET RESPONSE (T=0 sémantika)
- `6982` = security status not satisfied — potřeba auth (PIN, PACE, nebo SM)
- `6A82` = file not found (špatný FID)
- `6B00` = wrong P1/P2 (špatné parametry)
- `63CX` = wrong PIN, zbývá `X` pokusů (IOK má 3 default)
- `6983` = PIN zablokovaný po 3 špatných pokusech (potřeba DOK k odblokování)

**Co se věkového ověření týče:** `age_ok` **v API není**, protože DOB z čipu nečteme (2021+ karty mají ID cert za PACE/SM). `is_eobcanka: true` říká jen "je to Czech eID". Pokud potřebuješ skutečné věkové ověření, pojede se přes eDoklady/Bank iD přes QR flow — to je oficiální NIA-backed cesta.

**Časté chyby a fixy:**
- `"error": "no_card"` při VERIFY — karta vytažená během operace. Vlož znovu.
- `is_eobcanka: false` při vložené kartě — není to Czech eID (může to být bankovní platební karta nebo ePas). V logu `docker compose logs` uvidíš `Card inserted — eObčanka=False`.
- `reader_present: false` — pcscd na hostu neběží (`systemctl start pcscd`) nebo kontejner nemá bind-mount `/run/pcscd`.
- Kontejner nestartuje po přidání čtečky: obvykle chybí `/run/pcscd` na hostu (pcscd nebyl nainstalovaný). Compose bind-mount vytvoří prázdný adresář, ale bez socketu pyscard spadne. Fix: `sudo apt install pcscd && sudo systemctl start pcscd.socket`.

---

## 0a. Zapojení relé

**Deska:** SB Components Zero-Relay (2-channel, 5 V, pro Pi Zero form factor — ale funguje na libovolném RPi přes GPIO header).

**Piny (BCM):**

| Kanál | BCM pin | Physical pin | Použití                          |
|-------|---------|--------------|----------------------------------|
| R1    | GPIO 22 | 15           | Hlavní — ovládá automat          |
| R2    | GPIO 5  | 29           | Rezerva (zatím nevyužit)         |

**Polarita:** active-HIGH — GPIO HIGH = relé sepnuto, GPIO LOW = rozepnuto. Init pinů je LOW, takže při bootu / pádu kontejneru / odpojení RPi zůstane relé **rozepnuté**.

**Silová strana (svorkovnice R1):**

```
   Napájení 230 V / 24 V / 12 V ─── COM
                            NO ─── Zařízení (automat)
                            NC     (nezapojeno)
```

`COM + NO` zajišťuje **default-OFF**: bez proudu na cívce je obvod přerušen. `NC` schválně nepoužíváme — opačná polarita by při výpadku RPi automat zapnula.

**Ruční test před nasazením** (kontejner musí být zastavený, aby pin nedržel):

```bash
python3 scripts/relay-test.py            # interaktivní: 1=toggle, s=status, q=konec
python3 scripts/relay-test.py pulse 1 1  # sepne R1 na 1 s
```

LED na desce se musí rozsvítit při HIGH a zhasnout při LOW. Multimetrem ověř, že mezi COM a NO je při HIGH průchodnost (~0 Ω) a při LOW nekonečno.

---

---

## 1. Základní příprava OS

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl
```

---

## 2. Instalace Dockeru

```bash
# Oficiální Docker repo
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker $USER
```

**Odhlas se a přihlas znovu** (nebo reboot), ať má uživatel docker skupinu. Ověření:

```bash
docker --version
docker compose version
docker ps      # nemělo by vyžadovat sudo
```

---

## 3. Volba unikátního hostname pro Tailscale

Každé RPi musí mít v tailnetu vlastní hostname. Konvence:

- `rpi-vending` — první/master (už existuje)
- `rpi-vending-2`, `rpi-vending-3`, … — další instance
- nebo podle lokace, např. `rpi-trafika-praha`

```bash
export TS_HOSTNAME=rpi-vending-2   # nahraď svým
```

---

## 4. Instalace a připojení Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --hostname="$TS_HOSTNAME" --ssh
```

- `--ssh` = povolí Tailscale SSH (správa z jiného tailnet zařízení).
- V prohlížeči schval zařízení **stejným účtem** jako u ostatních RPi/VPS.

Ověření:

```bash
tailscale status
tailscale ip -4
```

---

## 5. Příprava adresáře a compose souboru

```bash
sudo mkdir -p /opt/trafika-rpi
sudo chown $USER:$USER /opt/trafika-rpi
cd /opt/trafika-rpi
```

Stáhni `docker-compose.yml` a `.env.example` z repa (bez klonování — stačí raw URL):

```bash
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/rpi/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/rpi/.env.example -o .env
mkdir -p data
```

---

## 6. Vygenerování tokenu a konfigurace `.env`

```bash
# Vygeneruj unikátní token pro toto zařízení:
openssl rand -base64 32 | tr -d '=+/' | cut -c1-43

# Vlož ho do .env (+ nastav DEVICE_NAME tak, aby odpovídal TS_HOSTNAME pro přehlednost):
nano .env
```

`.env` by měl vypadat přibližně takto:

```ini
WEBHOOK_TOKEN=<vygenerovaný token>
DEVICE_NAME=rpi-vending-2
PORT=8080
# Volitelné: numerická GID host skupiny `gpio` (default 997 = RPi OS).
# Zjisti `getent group gpio | cut -d: -f3`, změň jen když to host vrátí něco jiného.
GPIO_GID=997
```

> **Token si poznamenej** — stejnou hodnotu vložíš do `hub/rpis.yml` na VPS a budeš ho potřebovat v Odoo webhook konfiguraci pro tento konkrétní RPi.

---

## 7. Spuštění

```bash
docker compose pull      # stáhne :latest multiarch image z GHCR
docker compose up -d     # spustí na pozadí, restart=unless-stopped
docker compose ps
docker compose logs -f   # živé logy (Ctrl+C ukončí sledování, kontejner běží dál)
```

Autostart po rebootu máš zdarma díky `restart: unless-stopped` — Docker daemon službu pustí sám.

---

## 8. Ověření

Lokálně na RPi:

```bash
curl http://127.0.0.1:8080/api/health
curl http://127.0.0.1:8080/api/state
```

Z libovolného tailnet zařízení (VPS, notebook):

```bash
curl http://$TS_HOSTNAME:8080/api/state
curl -X POST -H "Authorization: Bearer <TOKEN>" http://$TS_HOSTNAME:8080/webhook/on
```

Dashboard v prohlížeči: **`http://<TS_HOSTNAME>:8080/`**.

---

## 9. Registrace do central hubu

Na VPS edituj `/opt/trafika-hub/rpis.yml` a přidej:

```yaml
- hostname: rpi-vending-2       # = $TS_HOSTNAME
  token: <WEBHOOK_TOKEN z .env>
  display_name: Trafika Praha
```

Pak restartuj hub:

```bash
cd /opt/trafika-hub && docker compose restart
```

Nové RPi se objeví v gridu na hub dashboardu (obvykle do ~3 s).

---

## Struktura souborů na RPi

```
/opt/trafika-rpi/
├── docker-compose.yml      Z repa, needitovat (změny uchováš v gitu)
├── .env                    Token + DEVICE_NAME + PORT (citlivé, chmod 600)
└── data/
    └── events.log          JSONL log událostí (bind-mount do /data v kontejneru)
```

---

## Endpointy

| Metoda | Cesta          | Auth  | Popis                                                              |
|--------|----------------|-------|--------------------------------------------------------------------|
| GET    | `/`            | ne    | Dashboard (HTML + JS polling 2 s)                                  |
| GET    | `/api/health`  | ne    | Health check (používá Docker HEALTHCHECK i hub)                    |
| GET    | `/api/state`   | ne    | Aktuální stav relé + device name                                   |
| GET    | `/api/status`  | ne    | Uptime, internet, disk, teplota, paměť, load + seznam `issues`     |
| GET    | `/api/device`  | ne    | Model RPi, sériové č., OS, kernel, RAM, hostname hostu, public IP  |
| GET    | `/api/logs`    | ne    | Posledních 100 událostí                                            |
| POST   | `/ui/toggle`   | ne    | Přepnutí z dashboardu / z hub toggle tlačítka                      |
| POST   | `/webhook/on`  | token | Externí signál ON (z Odoo, z hub ON tlačítka)                      |
| POST   | `/webhook/off` | token | Externí signál OFF                                                 |
| POST   | `/api/restart` | token | **Reboot celého hosta (RPi)** přes `reboot(2)` syscall. Nedostupné ~30-60 s. Fallback na restart kontejneru, když chybí CAP_SYS_BOOT. |
| GET    | `/qr`          | ne    | Stránka s rotujícím QR kódem (určená pro lokální displej u automatu).              |
| GET    | `/api/qr`      | ne    | Aktuální QR payload: `{device, token, url, rotate_at, rotate_seconds}`.            |

**Issue flags** (v `/api/status.issues[]`, používá hub pro „needs repair" badge):
- `no-internet` — RPi nedosáhne na 1.1.1.1 (DNS port 53). Pozn.: tailnet může pořád fungovat.
- `low-disk` — volné místo pod 10 %.
- `high-temp` — CPU teplota nad 80 °C.
- `high-memory` — využití RAM nad 90 %.

Endpointy bez tokenu spoléhají na to, že port 8080 je dostupný **jen přes tailnet**. Pokud někdy publikujeme port do internetu, přidat auth i na `/ui/*` a `/api/*`.

---

## Denní provoz (cheatsheet)

```bash
cd /opt/trafika-rpi

docker compose ps                # stav kontejneru
docker compose logs -f           # živé logy (Flask access log + service_start)
docker compose restart           # restart po změně .env
docker compose pull && docker compose up -d   # upgrade na nejnovější image
docker compose down              # zastavit (kontejner zmizí, data/ zůstane)

tail -f data/events.log          # JSON log událostí (přežívá restart)

tailscale status                 # tailnet peers
sudo tailscale down / up         # reconnect
```

---

## Fail-safe chování (DŮLEŽITÉ)

- Stav relé **vždy začíná na OFF** při každém startu kontejneru. Pokud RPi spadne / restartuje se / se aktualizuje image, automat zůstane vypnutý, dokud nepřijde explicitní ON signál z Odoo nebo hubu.
- Tuto vlastnost **nikdy neměň bez domluvy** — je to hlavní bezpečnostní požadavek projektu.

---

## Migrace z legacy systemd

Pokud na RPi běží původní systemd služba (`trafika-vending.service`), před nasazením Dockeru:

```bash
sudo systemctl stop trafika-vending.service
sudo systemctl disable trafika-vending.service
sudo rm /etc/systemd/system/trafika-vending.service
sudo systemctl daemon-reload

# Volitelné — archivace starých souborů a starého logu:
mv /home/varyshop/trafika/events.log /opt/trafika-rpi/data/events.log-legacy.jsonl 2>/dev/null || true
# Volitelně odstranit starou Python instalaci (pokud ji nepoužívá nic jiného):
# sudo apt remove python3-flask
```

Token ze starého `config.json` můžeš recyklovat jako `WEBHOOK_TOKEN` v novém `.env`, pokud ho už máš zaregistrovaný v hubu / Odoo.

---

## Troubleshooting

**`docker compose up -d` vrátí chybu „WEBHOOK_TOKEN is required":**
`.env` není ve stejném adresáři jako `docker-compose.yml`, nebo má prázdnou hodnotu. `docker compose config` ti ukáže, s jakými proměnnými compose pracuje.

**Kontejner restartuje v loopu:**
```bash
docker compose logs --tail=50
```
Obvykle chyba v `.env` (chybějící token) nebo port kolize (ať `ss -tlnp | grep 8080` ukáže, že port není obsazený jinou službou).

**Z jiného tailnet zařízení nejde curl na `http://<hostname>:8080`:**
1. `tailscale status` na obou stranách — jsou online?
2. Na RPi: `curl http://127.0.0.1:8080/api/state` — běží lokálně?
3. Firewall: `sudo iptables -L DOCKER-USER` — Docker maže své vlastní firewall pravidla, problém bývá v custom UFW blokací.

**Webhook vrací 401:**
Token v `Authorization: Bearer …` neodpovídá `WEBHOOK_TOKEN` v `.env`. Ověř: `docker compose exec trafika-rpi sh -c 'echo $WEBHOOK_TOKEN'`.

**Dashboard se nenačítá správně:**
Tvrdý reload (`Ctrl+Shift+R`) — HTML je inlined v image, cache prohlížeče může držet starou verzi po upgrade.

**Update image na novou verzi:**
```bash
docker compose pull && docker compose up -d
docker image prune   # úklid starých vrstev
```

---

## Co ještě není hotové (roadmapa)

Udržuj tento seznam — když se něco dotáhne, přesuň do Changelogu níž a doplň příslušné kroky v návodu.

- [ ] **Druhý relé kanál (R2 / GPIO5)** — zatím nevyužit. Rozšířit API na `?ch=1|2` až bude konkrétní use-case (osvětlení, chladič, samostatný bezpečnostní kontakt).
- [ ] **Integrace s Odoo** — automatic action / server action, která při loginu zavolá `POST /webhook/on` na správném `<hostname>` (mapování Odoo user → RPi hostname).
- [ ] **Způsob vypnutí** — nerozhodnuto: odhlášení v Odoo / timeout / fyzické tlačítko.
- [ ] **Rotace logů** — `data/events.log` roste donekonečna. Přidat logrotate na host, jakmile soubor začne být velký.
- [ ] **Produkční WSGI** — Flask dev server je OK na tailnetu, ale čistší by byl gunicorn (swap `CMD` v `rpi/Dockerfile`).

---

## Jak udržovat tento návod

- **Po každé změně `rpi/app.py`, `rpi/Dockerfile`, `docker-compose.yml` nebo postupu** edituj tento soubor a přidej záznam do Changelogu.
- Při nasazení nového RPi používej `:latest` tag. Pokud chceš konkrétní verzi, použij `:sha-<short>` nebo `:v<semver>` — dostupné tagy vidíš v GHCR: https://github.com/michalvarys/rpi-vending-controller/pkgs/container/trafika-rpi
- Když se něco z roadmapy hotově — přesuň položku z „Co ještě není hotové" do Changelogu.

---

## Changelog

- **2026-04-21** — Přidán contact eObčanka reader (AXAGON CRE-SM3TC nebo libovolná PC/SC čtečka). Po vložení karty se SELECT card-mgmt AID; pokud je to eObčanka (SW=9000), spustí se relé na `RELAY_ON_SECONDS` a auto-off timer ho pak vypne. Nové soubory: `rpi/card_reader.py` (pyscard wrapper), bind-mount `/run/pcscd` v compose, apt deps `pcscd libccid pcsc-tools` na hostu (řeší `install.sh`), apt deps `python3-pyscard python3-cryptography` v image. Nový `/api/card/state` endpoint. Kiosk `/qr` stránka polluje `/api/state` a při `ON` zobrazí zelený success overlay s odpočtem. **DOB/věk z čipu nečteme** — 2021+ eObčanky mají ID cert za PACE/SM, implementace by byla 1-2 týdny krypto; kontaktní čtečka zde slouží jen jako rychlé "vložení karty" UX (operator lokace) místo hledání QR v telefonu. Pro právní ověření věku je korektní cesta eDoklady/Bank iD přes existující QR/NIA flow.
- **2026-04-20** — `install.sh` nastavuje GPIO automaticky: detekuje `GPIO_GID` přes `getent group gpio`, zapíše ho do `.env`, přidá sudo-usera do skupiny `gpio` (aby host `scripts/relay-test.py` fungoval bez `sudo`) a po deploy ověří, že kontejner naběhl v `relay=gpio` módu (ne `mock`) — parsuje `data/events.log`. Při mock módu vyhodí warning s debug commandy.
- **2026-04-20** — Dockerfile: `lgpio` pip balíček potřebuje `liblgpio.so`, který není v Debian bookworm ani trixie (balíček `liblgpio1` v repech neexistuje). Řešení: base přepnut na `python:3.13-slim-trixie`, `liblgpio` se klonuje a buildí ze zdroje z `github.com/joan2937/lg` přímo v `RUN` layeru (git clone → make → make install → ldconfig → purge build-deps). Nová env `GPIOZERO_PIN_FACTORY=lgpio` aby gpiozero šel rovnou na lgpio factory (bez fallback kaskády).
- **2026-04-20** — Fix `group_add: gpio` → v compose nyní numerická GID (`${GPIO_GID:-997}`), protože slim Python image `gpio` skupinu nemá a resolve selhával (`Unable to find group gpio`). Nová volitelná env `GPIO_GID` v `.env` (default 997 = RPi OS; zjisti `getent group gpio | cut -d: -f3`).
- **2026-04-20** — Reálné GPIO pro kanál 1 (R1 = BCM 22, active-HIGH, init LOW). `rpi/app.py` drží `gpiozero.OutputDevice` a `set_relay()` doopravdy spíná cívku — stav se už nejen loguje, ale fyzicky přepíná. Dockerfile přidal `gpiozero` + `lgpio`. Compose zapnul `devices: [/dev/gpiomem, /dev/gpiochip0]` a `group_add: [gpio]`. Nové env proměnné `RELAY_GPIO` (default 22) a `RELAY_ACTIVE_HIGH` (default true) — jen pro případ jiné wiring na konkrétním RPi. Fail-safe: atexit + SIGTERM handler + explicit OFF před `/api/restart` host rebootem. Pokud init GPIO selže (dev stroj, chybějící `/dev/gpiochip*`), app naběhne v **mock módu** s varováním do stderr — tj. dashboard a API fungují, jen se fyzicky nic nespíná. Ruční bench-test: `scripts/relay-test.py`. Přidána sekce **0a. Zapojení relé** s pinout tabulkou a COM+NO schématem.
- **2026-04-19** — Volitelný kiosk mód. Nový `scripts/install-kiosk.sh` — na Pi OS Desktop (Bookworm labwc/LXDE) zajistí, že po bootu se automaticky otevře `http://localhost:8080/qr` ve fullscreen Chromium. Doinstaluje chromium + unclutter, vytvoří wrapper skript, XDG autostart entry, zapne desktop autologin přes raspi-config. Headless instalace bez displeje script nepotřebuje.
- **2026-04-18** — QR aktivační flow. Nové endpointy `/qr` (stránka s live QR) a `/api/qr` (aktuální token). Token = HMAC-SHA256(`WEBHOOK_TOKEN`, `hostname:floor(now/60)`), rotuje každých `QR_ROTATE_SECONDS` (default 60 s). Dvě nové env proměnné: `QR_BASE_URL` (veřejná URL shopu) a `QR_ROTATE_SECONDS`. QR vede na `<QR_BASE_URL>/activate/<hostname>/<token>` — ta trasa patří do reálného Odoo modulu (mock implementace v `shop-mock/`). Hub má `POST /api/qr/validate` s identickým HMAC výpočtem.
- **2026-04-18** — `/api/restart` nyní **rebootuje celé RPi** (ne jen kontejner). Používá `reboot(2)` syscall přes ctypes; compose nově přidává `cap_add: [SYS_BOOT]`. Při chybějící capability se degraduje na restart kontejneru. UI tooltip a confirm dialog v hubu aktualizované.
- **2026-04-18** — Remote restart. Nový endpoint `POST /api/restart` (token-protected) — zaloguje událost, odpoví 200, po 1 s zavolá `os._exit(0)`. Docker `restart: unless-stopped` kontejner pustí znovu; relé přejde do defaultního OFF. V hub UI nové tlačítko `↻` s confirm dialogem.
- **2026-04-18** — Reporting stavu a HW info. Přidány `/api/status` (uptime, internet ping, disk, teplota, RAM, load, issue flags) a `/api/device` (model, sériové č., host OS, kernel, public IP, LOCATION z env). Compose bind-mountuje `/etc/os-release` a `/etc/hostname` z hostu. Přibyla `LOCATION` env proměnná (volitelná). Hub grid karty ukazují health badge, metriky a „Info o zařízení". `install.sh` se ptá na polohu.
- **2026-04-18** — Přidán `scripts/install.sh` — jeden-liner instalace pro čisté RPi (apt, Docker, Tailscale, compose, start, tisk YAML bloku pro hub). Idempotentní.
- **2026-04-18** — Migrace na Docker: image `ghcr.io/michalvarys/trafika-rpi`, konfigurace přes `.env`, bind-mount `./data` pro event log, HEALTHCHECK v image. `app.py` přestal generovat `config.json` — token jen z env. Přibyl `/api/health` endpoint. Původní apt+systemd instalace dostupná v gitu před tímto commitem (v `/home/varyshop/trafika/` na master RPi jako zkumavkový stav během migrace).
- **2026-04-18** — Počáteční verze (apt+systemd). Tailscale + Flask webhook + dashboard + systemd, mock GPIO. Master: `rpi-vending` (100.71.128.86). VPS: `varyshop-trafika-vps` (100.66.209.58).
