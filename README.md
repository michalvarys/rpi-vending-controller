# Trafika vending controller

Dockerized systém pro bezpečné zapínání napájení vending automatů v trafikách. Raspberry Pi v každé trafice spíná relé jen na signál z backendu. Centrální hub na VPS dává přehled a manuální kontrolu nad všemi RPi.

## Komponenty

| Adresář      | Popis | Image |
|--------------|-------|-------|
| `rpi/`       | Flask app běžící na každém Raspberry Pi — přijímá webhooky, řídí relé, má vlastní dashboard. | `ghcr.io/michalvarys/trafika-rpi` |
| `hub/`       | Flask app běžící na VPS — agreguje stav všech RPi, dává grid UI, proxy pro manuální ovládání. | `ghcr.io/michalvarys/trafika-hub` |
| `shop-mock/` | Mock login UI na VPS — supluje Odoo, demonstruje flow po přihlášení uživatele do shopu. | `ghcr.io/michalvarys/trafika-shop-mock` |

Síť mezi RPi a hubem je **Tailscale** — RPi v trafikách jsou za NAT, Tailscale vyřeší tunel.

## Nasazení

- **Nový RPi** → [SETUP.md](SETUP.md)
- **Hub na VPS** → [HUB-SETUP.md](HUB-SETUP.md)
- **Shop-mock na VPS** → [SHOP-MOCK-SETUP.md](SHOP-MOCK-SETUP.md)

## Bezpečnostní pravidlo

Relé musí být **VŽDY OFF po startu** služby. Nikdy se nezapíná samo — jen po explicitním webhook signálu z Odoo nebo manuálně z dashboardu. Pokud RPi spadne / restartuje se / se aktualizuje, automat zůstane bez napájení.
