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

- [ ] **Reálné GPIO** — `set_relay()` v `rpi/app.py` je mock. Jakmile bude potvrzený pin a typ relé (active-HIGH/LOW), nahradit `gpiozero.OutputDevice(pin, active_high=..., initial_value=False)` a odkomentovat v `docker-compose.yml` bloky `devices:` (`/dev/gpiomem`) a `group_add: [gpio]`. Do tohoto návodu pak přidat, jaký pin je na kterém RPi.
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

- **2026-04-19** — Volitelný kiosk mód. Nový `scripts/install-kiosk.sh` — na Pi OS Desktop (Bookworm labwc/LXDE) zajistí, že po bootu se automaticky otevře `http://localhost:8080/qr` ve fullscreen Chromium. Doinstaluje chromium + unclutter, vytvoří wrapper skript, XDG autostart entry, zapne desktop autologin přes raspi-config. Headless instalace bez displeje script nepotřebuje.
- **2026-04-18** — QR aktivační flow. Nové endpointy `/qr` (stránka s live QR) a `/api/qr` (aktuální token). Token = HMAC-SHA256(`WEBHOOK_TOKEN`, `hostname:floor(now/60)`), rotuje každých `QR_ROTATE_SECONDS` (default 60 s). Dvě nové env proměnné: `QR_BASE_URL` (veřejná URL shopu) a `QR_ROTATE_SECONDS`. QR vede na `<QR_BASE_URL>/activate/<hostname>/<token>` — ta trasa patří do reálného Odoo modulu (mock implementace v `shop-mock/`). Hub má `POST /api/qr/validate` s identickým HMAC výpočtem.
- **2026-04-18** — `/api/restart` nyní **rebootuje celé RPi** (ne jen kontejner). Používá `reboot(2)` syscall přes ctypes; compose nově přidává `cap_add: [SYS_BOOT]`. Při chybějící capability se degraduje na restart kontejneru. UI tooltip a confirm dialog v hubu aktualizované.
- **2026-04-18** — Remote restart. Nový endpoint `POST /api/restart` (token-protected) — zaloguje událost, odpoví 200, po 1 s zavolá `os._exit(0)`. Docker `restart: unless-stopped` kontejner pustí znovu; relé přejde do defaultního OFF. V hub UI nové tlačítko `↻` s confirm dialogem.
- **2026-04-18** — Reporting stavu a HW info. Přidány `/api/status` (uptime, internet ping, disk, teplota, RAM, load, issue flags) a `/api/device` (model, sériové č., host OS, kernel, public IP, LOCATION z env). Compose bind-mountuje `/etc/os-release` a `/etc/hostname` z hostu. Přibyla `LOCATION` env proměnná (volitelná). Hub grid karty ukazují health badge, metriky a „Info o zařízení". `install.sh` se ptá na polohu.
- **2026-04-18** — Přidán `scripts/install.sh` — jeden-liner instalace pro čisté RPi (apt, Docker, Tailscale, compose, start, tisk YAML bloku pro hub). Idempotentní.
- **2026-04-18** — Migrace na Docker: image `ghcr.io/michalvarys/trafika-rpi`, konfigurace přes `.env`, bind-mount `./data` pro event log, HEALTHCHECK v image. `app.py` přestal generovat `config.json` — token jen z env. Přibyl `/api/health` endpoint. Původní apt+systemd instalace dostupná v gitu před tímto commitem (v `/home/varyshop/trafika/` na master RPi jako zkumavkový stav během migrace).
- **2026-04-18** — Počáteční verze (apt+systemd). Tailscale + Flask webhook + dashboard + systemd, mock GPIO. Master: `rpi-vending` (100.71.128.86). VPS: `varyshop-trafika-vps` (100.66.209.58).
