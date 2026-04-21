#!/usr/bin/env bash
#
# Trafika vending controller — one-shot installer pro čisté Raspberry Pi.
# https://github.com/michalvarys/rpi-vending-controller
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/scripts/install.sh | sudo bash
#
# Nebo stáhnout a spustit (doporučeno — ať vidíš, co script dělá):
#   curl -fsSL https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main/scripts/install.sh -o install.sh
#   sudo bash install.sh
#
# Env proměnné (vše volitelné — co není nastavené, na to se script zeptá):
#   TS_HOSTNAME          Tailscale hostname (např. rpi-vending-praha)
#   DEVICE_NAME          Popisek do UI (default: $TS_HOSTNAME)
#   DISPLAY_NAME         Název pro hub grid (default: $DEVICE_NAME)
#   WEBHOOK_TOKEN        Pokud chceš recyklovat existující token (default: vygeneruje nový)
#   TAILSCALE_AUTH_KEY   Pre-auth key → neinteraktivní Tailscale login
#   PORT                 Host port (default: 8080)

set -euo pipefail

: "${TS_HOSTNAME:=}"
: "${DEVICE_NAME:=}"
: "${DISPLAY_NAME:=}"
: "${LOCATION:=}"
: "${WEBHOOK_TOKEN:=}"
: "${TAILSCALE_AUTH_KEY:=}"
: "${QR_BASE_URL:=}"
: "${QR_ROTATE_SECONDS:=60}"
: "${PORT:=8080}"

INSTALL_DIR="/opt/trafika-rpi"
IMAGE="ghcr.io/michalvarys/trafika-rpi:latest"
REPO_RAW="https://raw.githubusercontent.com/michalvarys/rpi-vending-controller/main"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

prompt() {
  local var="$1" default="${2:-}" msg="$3"
  if [[ -n "${!var:-}" ]]; then
    return
  fi
  local val
  local tty_source=/dev/stdin
  [[ -t 0 ]] || tty_source=/dev/tty
  local suffix=""
  [[ -n "$default" ]] && suffix=" [$default]"
  read -r -p "$msg$suffix: " val <"$tty_source"
  printf -v "$var" '%s' "${val:-$default}"
  export "$var"
}

# --- 1. Root check ---
[[ $EUID -eq 0 ]] || die "Spusť jako root: 'sudo bash install.sh' nebo 'curl ... | sudo bash'"

# --- 2. Načti existující .env, ať zachovám hodnoty při re-runu ---
if [[ -f "$INSTALL_DIR/.env" ]]; then
  log "Nalezen existující $INSTALL_DIR/.env — zachovávám hodnoty, které nejsou přepsané env proměnnou."
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Z_]+$ ]] || continue
    [[ -n "${!k:-}" ]] && continue
    printf -v "$k" '%s' "$v"
    export "$k"
  done < "$INSTALL_DIR/.env"
fi

# --- 3. Základní balíčky ---
log "apt update + curl/ca-certificates/openssl"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates openssl gnupg

# --- 4. Docker ---
if ! command -v docker >/dev/null 2>&1; then
  log "Instaluji Docker z oficiálního repa..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $codename stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  if [[ -n "${SUDO_USER:-}" ]]; then
    usermod -aG docker "$SUDO_USER" || true
    log "Uživatel '$SUDO_USER' přidán do skupiny 'docker' — aktivní po příštím přihlášení."
  fi
else
  log "Docker už je nainstalovaný ($(docker --version))"
fi

# --- 4b. pcscd (smart card daemon pro eObčanka čtečku) ---
# Nainstaluje daemon na hostu; kontejner ho používá přes bind-mountovaný socket /run/pcscd.
# Pokud čtečka není zapojená, pcscd prostě běží naprázdno — nic nerozbije.
if ! command -v pcscd >/dev/null 2>&1; then
  log "Instaluji pcscd + CCID driver (pro AXAGON CRE-SM3TC a jiné PC/SC čtečky)..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq pcscd libccid pcsc-tools
else
  log "pcscd už je nainstalovaný."
fi
systemctl enable --now pcscd.socket >/dev/null 2>&1 || systemctl enable --now pcscd >/dev/null 2>&1 || warn "pcscd službu se nepodařilo zapnout — zkontroluj 'systemctl status pcscd'."
if command -v pcsc_scan >/dev/null 2>&1; then
  readers_out=$(timeout 3 pcsc_scan -n 2>/dev/null | head -5 || true)
  if [[ -n "$readers_out" && "$readers_out" != *"No reader"* ]]; then
    log "✓ Čtečka detekována."
  else
    warn "Čtečka není detekována — zapoj AXAGON CRE-SM3TC přes USB, pak spusť 'pcsc_scan'."
  fi
fi

# --- 5. Tailscale ---
if ! command -v tailscale >/dev/null 2>&1; then
  log "Instaluji Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
else
  log "Tailscale už je nainstalovaný ($(tailscale version | head -1))"
fi

# --- 6. Hostname & připojení Tailscale ---
if tailscale status --json >/dev/null 2>&1; then
  current_hostname=$(tailscale status --json 2>/dev/null | grep -oP '"HostName":\s*"\K[^"]+' | head -1)
  log "Tailscale už je přihlášen jako '$current_hostname' — nepřelogovávám."
  TS_HOSTNAME="${TS_HOSTNAME:-$current_hostname}"
else
  prompt TS_HOSTNAME "rpi-vending-new" "Tailscale hostname pro tento RPi"
  log "Připojuji Tailscale s hostname '$TS_HOSTNAME'..."
  if [[ -n "$TAILSCALE_AUTH_KEY" ]]; then
    tailscale up --hostname="$TS_HOSTNAME" --ssh --authkey="$TAILSCALE_AUTH_KEY"
  else
    warn "Žádný TAILSCALE_AUTH_KEY — otevře se URL, kterou musíš schválit v browseru."
    tailscale up --hostname="$TS_HOSTNAME" --ssh
  fi
fi

# --- 7. Config: DEVICE_NAME, DISPLAY_NAME, LOCATION, token ---
DEVICE_NAME="${DEVICE_NAME:-$TS_HOSTNAME}"
DISPLAY_NAME="${DISPLAY_NAME:-$DEVICE_NAME}"
prompt LOCATION "" "Poloha (např. 'Praha, Vinohrady' — lze nechat prázdné)"
prompt QR_BASE_URL "https://automaty.elite-trafika.cz" "URL shopu pro QR aktivaci"

if [[ -z "$WEBHOOK_TOKEN" ]]; then
  WEBHOOK_TOKEN=$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-43)
  log "Vygenerován nový WEBHOOK_TOKEN."
else
  log "Používám existující WEBHOOK_TOKEN (z env nebo staré .env)."
fi

# --- 8. GPIO skupina (pro relé kontejner) ---
# Container užívá `group_add: ${GPIO_GID:-997}` v compose. Na RPi OS je to většinou 997,
# ale zjistit aktuální hodnotu z host systému je spolehlivější (jiná RPi OS verze to může mít jinak).
GPIO_GID=$(getent group gpio 2>/dev/null | cut -d: -f3 || true)
if [[ -z "$GPIO_GID" ]]; then
  warn "Skupina 'gpio' na hostu neexistuje — GPIO řízení relé pravděpodobně nebude fungovat. Na RPi OS ji vytváří `raspi-config` / kernel udev pravidla."
  GPIO_GID=997
fi
log "Host gpio skupina má GID $GPIO_GID."

# Sudo user do gpio skupiny, ať může sáhnout na /dev/gpio* i z hosta (scripts/relay-test.py).
if [[ -n "${SUDO_USER:-}" ]] && getent group gpio >/dev/null 2>&1; then
  if ! id -nG "$SUDO_USER" | tr ' ' '\n' | grep -qx gpio; then
    usermod -aG gpio "$SUDO_USER" || true
    log "Uživatel '$SUDO_USER' přidán do skupiny 'gpio' — aktivní po příštím přihlášení."
  fi
fi

# --- 9. Adresář + compose + .env ---
log "Nastavuju $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/data"
curl -fsSL "$REPO_RAW/rpi/docker-compose.yml" -o "$INSTALL_DIR/docker-compose.yml"

cat > "$INSTALL_DIR/.env" <<ENVFILE
WEBHOOK_TOKEN=$WEBHOOK_TOKEN
DEVICE_NAME=$DEVICE_NAME
LOCATION="$LOCATION"
QR_BASE_URL=$QR_BASE_URL
QR_ROTATE_SECONDS=$QR_ROTATE_SECONDS
PORT=$PORT
GPIO_GID=$GPIO_GID
# Age-gate přes kontaktní čtečku eObčanky. `auto` (default) = zapni, když pyscard najde
# čtečku; `false` = úplně vypni (jen QR flow); `true` = vynuť (chyba čtečky = issue flag).
CARD_READER_ENABLED=auto
AGE_THRESHOLD=18
RELAY_ON_SECONDS=60
ENVFILE
chmod 600 "$INSTALL_DIR/.env"

if [[ -n "${SUDO_USER:-}" ]]; then
  chown -R "$SUDO_USER:$SUDO_USER" "$INSTALL_DIR"
fi

# --- 10. Pull + run ---
log "Stahuju image $IMAGE a spouštím kontejner..."
( cd "$INSTALL_DIR" && docker compose pull && docker compose up -d )

# --- 11. Health check ---
log "Čekám na healthcheck..."
for i in $(seq 1 20); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    log "✓ /api/health odpovídá."
    break
  fi
  sleep 1
  if [[ $i -eq 20 ]]; then
    warn "/api/health neodpovídá po 20 s. Zkontroluj 'docker compose logs' v $INSTALL_DIR."
  fi
done

# --- 12. GPIO mode check — relay=gpio nebo relay=mock? ---
if [[ -f "$INSTALL_DIR/data/events.log" ]]; then
  relay_mode=$(grep -o 'relay=[a-z]*' "$INSTALL_DIR/data/events.log" 2>/dev/null | tail -1 | cut -d= -f2)
  if [[ "$relay_mode" == "gpio" ]]; then
    log "✓ Kontejner běží v GPIO módu — relé se bude reálně spínat."
  elif [[ "$relay_mode" == "mock" ]]; then
    warn "Kontejner naběhl v mock módu (GPIO init selhal). Zkontroluj:"
    warn "  docker compose logs trafika-rpi | grep -i gpio"
    warn "  /dev/gpiochip0 a /dev/gpiomem musí být vidět z kontejneru."
  fi
fi

# --- 13. Summary + YAML blok pro hub ---
cat <<SUMMARY

================================================================
  Hotovo. Trafika RPi je zprovozněná.

  Dashboard (z libovolného tailnet zařízení):
    http://$TS_HOSTNAME:$PORT/

  Token a konfigurace:
    $INSTALL_DIR/.env  (chmod 600)

  ─── Registrace do hubu ───
  Na VPS otevři /opt/trafika-hub/rpis.yml a přidej tento blok:

    - hostname: $TS_HOSTNAME
      token: $WEBHOOK_TOKEN
      display_name: $DISPLAY_NAME
      port: $PORT

  Pak na VPS:  cd /opt/trafika-hub && docker compose restart

  ─── Užitečné příkazy ───
    cd $INSTALL_DIR
    docker compose logs -f          # živé logy
    docker compose pull && docker compose up -d   # upgrade
    tail -f data/events.log         # JSON log událostí
================================================================

SUMMARY
