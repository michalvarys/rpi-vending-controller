#!/usr/bin/env python3
"""Interaktivní test SB Components Zero-Relay 2-ch boardu.

Piny (BCM):  R1 = GPIO22 (phys 15), R2 = GPIO5 (phys 29)
Polarita:    active-HIGH (HIGH = sepnuto, LOW = rozepnuto)

Použití:
  sudo python3 relay-test.py               # interaktivní (1/2/a/q)
  sudo python3 relay-test.py on 1          # R1 HIGH
  sudo python3 relay-test.py off 2         # R2 LOW
  sudo python3 relay-test.py pulse 1 0.5   # R1 sepnout na 0.5 s

Exit vždy nastaví oba piny LOW (bezpečný stav).
"""
import sys
import time

try:
    from gpiozero import OutputDevice
except ImportError:
    print("Chybí gpiozero. Nainstaluj:  sudo apt install -y python3-gpiozero", file=sys.stderr)
    sys.exit(1)

PINS = {1: 22, 2: 5}  # BCM

relays = {
    ch: OutputDevice(pin, active_high=True, initial_value=False)
    for ch, pin in PINS.items()
}


def status():
    return " | ".join(f"R{ch}={'ON ' if r.value else 'OFF'}" for ch, r in relays.items())


def set_channel(ch, on):
    if ch not in relays:
        print(f"Neznámý kanál {ch} (použij 1 nebo 2)")
        return
    (relays[ch].on if on else relays[ch].off)()
    print(f"R{ch} -> {'HIGH (ON)' if on else 'LOW (OFF)'}   [{status()}]")


def pulse(ch, seconds):
    set_channel(ch, True)
    time.sleep(seconds)
    set_channel(ch, False)


def interactive():
    print("SB Zero-Relay test — 1=toggle R1, 2=toggle R2, a=oba OFF, q=quit")
    print(f"Start: {status()}")
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "1":
            set_channel(1, not relays[1].value)
        elif cmd == "2":
            set_channel(2, not relays[2].value)
        elif cmd == "a":
            set_channel(1, False)
            set_channel(2, False)
        elif cmd == "s":
            print(status())
        elif cmd:
            print("1 / 2 = toggle, a = oba OFF, s = status, q = quit")


def main():
    try:
        args = sys.argv[1:]
        if not args:
            interactive()
            return
        action = args[0].lower()
        if action in ("on", "off") and len(args) >= 2:
            set_channel(int(args[1]), action == "on")
        elif action == "pulse" and len(args) >= 2:
            dur = float(args[2]) if len(args) >= 3 else 0.5
            pulse(int(args[1]), dur)
        elif action == "status":
            print(status())
        else:
            print(__doc__)
            sys.exit(2)
    finally:
        for r in relays.values():
            r.off()
        print(f"Konec: {status()}")


if __name__ == "__main__":
    main()
