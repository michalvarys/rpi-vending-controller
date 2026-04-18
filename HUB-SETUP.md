# Trafika central hub — nasazení na VPS

Central hub agreguje stav všech Raspberry Pi controllerů, ukazuje je v gridu a umožňuje manuální ON/OFF/Toggle z jednoho místa. Běží jako Docker kontejner na VPS, který je v tailnetu (`varyshop-trafika-vps`).

---

## 0. Prerekvizity

- VPS s Debianem/Ubuntu a veřejnou IP (ale dashboard **nebudeme publikovat navenek** — přístup jen přes tailnet).
- Docker + compose plugin nainstalované (`docker --version && docker compose version`).
- VPS je v tailnetu jako `varyshop-trafika-vps` (viz reference_tailnet memory / tvé Tailscale admin konzole).
- Alespoň jedno RPi už běží dle `SETUP.md` a má známý hostname + `WEBHOOK_TOKEN`.

---

## 1. Příprava adresáře

```bash
sudo mkdir -p /opt/trafika-hub
sudo chown $USER:$USER /opt/trafika-hub
cd /opt/trafika-hub
```

---

## 2. Stažení compose souboru a konfigurace

```bash
curl -fsSL https://raw.githubusercontent.com/michalvarys/trafika/main/hub/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/michalvarys/trafika/main/hub/.env.example -o .env
curl -fsSL https://raw.githubusercontent.com/michalvarys/trafika/main/hub/rpis.yml.example -o rpis.yml
```

Edituj `rpis.yml` — přidej všechny RPi, které chceš spravovat:

```yaml
rpis:
  - hostname: rpi-vending
    token: <WEBHOOK_TOKEN z rpi-vending:/opt/trafika-rpi/.env>
    display_name: Trafika Brno
    port: 8080

  - hostname: rpi-vending-2
    token: <WEBHOOK_TOKEN z rpi-vending-2:/opt/trafika-rpi/.env>
    display_name: Trafika Praha
```

**Důležité:** `hostname` musí odpovídat Tailscale MagicDNS jménu daného RPi. `token` musí být přesně ten samý, co má RPi ve svém `.env` (hub ho používá pro webhook ON/OFF volání).

`.env` zpravidla editovat nemusíš — defaulty (port 8080, poll každé 3 s) jsou rozumné.

---

## 3. Spuštění

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

V logách uvidíš `Loaded N RPi(s) from /config/rpis.yml`.

---

## 4. Ověření

```bash
curl http://127.0.0.1:8080/api/health
curl http://127.0.0.1:8080/api/dashboard | python3 -m json.tool
```

V `api/dashboard` by měl každý RPi mít `"reachable": true` a naplněný `"state"` (do ~3 s po startu hubu).

---

## 5. Dashboard v prohlížeči

Z libovolného tailnet zařízení:

**`http://varyshop-trafika-vps:8080/`**

Uvidíš grid karet — jedna karta per RPi, s:

- Velkou barvou tečkou (zelená = ON, červená = OFF, šedá = unreachable)
- Názvem + hostname
- Časem poslední změny + kdo ji spustil (`webhook` = Odoo, `hub` = toto UI, `dashboard` = lokální RPi UI)
- Tlačítky **ON / OFF / Toggle** (disabled pokud RPi neodpovídá)
- Rozbalitelným logem posledních 20 událostí
- Odkazem na lokální dashboard toho RPi (`↗`)

Auto-refresh 3 s.

---

## 6. Přidání nového RPi do hubu

1. Nový RPi musí už běžet dle `SETUP.md` (Docker + Tailscale + vlastní token v `.env`).
2. Na VPS edituj `/opt/trafika-hub/rpis.yml` — přidej nový blok.
3. Restartuj hub:
   ```bash
   cd /opt/trafika-hub && docker compose restart
   ```
4. Karta by se měla v UI objevit do ~3 s.

> `rpis.yml` **není v imagi** — je bind-mountovaný z hosta, takže změny přežívají upgrade. Image updatovat nezávisle.

---

## Endpointy

| Metoda | Cesta                             | Popis                                                         |
|--------|-----------------------------------|---------------------------------------------------------------|
| GET    | `/`                               | Dashboard (grid všech RPi, auto-refresh 3 s)                  |
| GET    | `/api/health`                     | Health check + počet registrovaných RPi                       |
| GET    | `/api/dashboard`                  | JSON se stavem všech RPi (co vidí dashboard)                  |
| POST   | `/api/rpi/<hostname>/on`          | Proxy na `/webhook/on` daného RPi (hub dosadí token)          |
| POST   | `/api/rpi/<hostname>/off`         | Proxy na `/webhook/off`                                       |
| POST   | `/api/rpi/<hostname>/toggle`      | Proxy na `/ui/toggle` (bez tokenu)                            |

Hub neřeší autentizaci návštěvníků dashboardu — spoléhá na to, že port 8080 je vystavený **jen přes tailnet**. Pokud by se někdy publikoval do internetu, přidat auth.

---

## Denní provoz (cheatsheet)

```bash
cd /opt/trafika-hub

docker compose ps
docker compose logs -f
docker compose restart              # po editaci rpis.yml
docker compose pull && docker compose up -d   # upgrade image
docker compose down                 # zastavit

# rychlý test některého RPi:
curl -X POST http://127.0.0.1:8080/api/rpi/rpi-vending/toggle
```

---

## Troubleshooting

**RPi v UI má `OFFLINE` / unreachable:**
1. Z VPS `ping <hostname>` — funguje tailnet?
2. Z VPS `curl http://<hostname>:8080/api/health` — odpovídá RPi Flask?
3. Zkontroluj logy RPi (`docker compose logs -f` na RPi) — neodpadl kontejner?
4. Hub poll timeout: default 4 s. Pokud máš pomalé spojení, zvedni `POLL_TIMEOUT` v `.env` a restartuj hub.

**Tlačítko ON vrátí „502" s `Connection refused`:**
RPi neodpovídá. Viz bod výše. Hub se chová správně — nesnaží se 502 zamaskovat.

**Tlačítko ON vrátí „502" s `401 Unauthorized` v chybě:**
Token v `rpis.yml` je jiný, než má RPi v `.env`. Synchronizuj je a restartuj hub.

**Nové RPi se po restartu neobjeví:**
```bash
docker compose config       # ukáže, jestli se rpis.yml bind-mountuje správně
docker compose exec trafika-hub cat /config/rpis.yml
```
Pokud je soubor prázdný / starý, zkontroluj `volumes:` sekci v `docker-compose.yml`.

**Dashboard nahoře vpravo ukazuje „Hub API unreachable":**
Docker kontejner neběží nebo spadl do restart loopu. `docker compose logs` ukáže důvod.

---

## Jak udržovat tento návod

- Po změně `hub/app.py`, `hub/Dockerfile`, `hub/docker-compose.yml` nebo workflow pro registraci RPi → edituj tento soubor a přidej Changelog entry.
- `rpis.yml` na VPS je produkční data — **nekopíruj jeho obsah do gitu**. V repu je jen `rpis.yml.example`.

---

## Changelog

- **2026-04-18** — Počáteční verze hubu. Image `ghcr.io/michalvarys/trafika-hub`, YAML registry RPi, poll interval 3 s, grid UI s per-RPi kartami, proxy endpointy pro ON/OFF/Toggle.
