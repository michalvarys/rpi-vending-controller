#!/usr/bin/env python3
"""Bench-test pro relé deska připojené přes GPIO.

Defaulty pasované na **Waveshare RPi Relay Board (B), 8-channel, active-LOW**.
Pro jiné desky přepiš RELAY_GPIOS / ACTIVE_HIGH přes environment nebo --pins / --high.

Pinout (BCM): R1=5, R2=6, R3=13, R4=16, R5=19, R6=20, R7=21, R8=26

Použití:
  sudo python3 relay-test.py                # interaktivní (1-8/a/A/q)
  sudo python3 relay-test.py on 3           # R3 zapni (active-LOW = pin LOW)
  sudo python3 relay-test.py off 3
  sudo python3 relay-test.py all on         # všech 8 ON
  sudo python3 relay-test.py all off        # všech 8 OFF
  sudo python3 relay-test.py pulse 1 0.5    # R1 sepnout na 0.5 s
  sudo python3 relay-test.py sweep          # postupně 1..8 cvakni
  RELAY_GPIOS=22 RELAY_HIGH=1 python3 relay-test.py    # SB Zero-Relay 1ch active-HIGH

Konec vždy nastaví všechny piny do OFF (bezpečný stav).
"""
import os
import sys
import time

try:
    from gpiozero import OutputDevice
except ImportError:
    print("Chybí gpiozero. Nainstaluj:  sudo apt install -y python3-gpiozero", file=sys.stderr)
    sys.exit(1)


def _parse_pins(s):
    return [int(p.strip()) for p in s.split(",") if p.strip()]


PINS = _parse_pins(os.environ.get("RELAY_GPIOS", "5,6,13,16,19,20,21,26"))
ACTIVE_HIGH = os.environ.get("RELAY_HIGH", "0").strip().lower() in ("1", "true", "yes")

# CLI override
if "--pins" in sys.argv:
    i = sys.argv.index("--pins")
    PINS = _parse_pins(sys.argv[i + 1])
    del sys.argv[i:i + 2]
if "--high" in sys.argv:
    ACTIVE_HIGH = True
    sys.argv.remove("--high")

relays = {
    idx + 1: OutputDevice(pin, active_high=ACTIVE_HIGH, initial_value=False)
    for idx, pin in enumerate(PINS)
}


def status():
    polarity = "active-HIGH" if ACTIVE_HIGH else "active-LOW"
    parts = " ".join(f"R{ch}={'ON' if r.value else '..'}" for ch, r in relays.items())
    return f"{parts}   ({polarity})"


def set_channel(ch, on):
    if ch not in relays:
        print(f"Neznámý kanál {ch} (mám 1-{len(relays)})")
        return
    (relays[ch].on if on else relays[ch].off)()
    print(f"R{ch} -> {'ON' if on else 'OFF'}   [{status()}]")


def all_channels(on):
    for r in relays.values():
        (r.on if on else r.off)()
    print(f"all -> {'ON' if on else 'OFF'}   [{status()}]")


def pulse(ch, seconds):
    set_channel(ch, True)
    time.sleep(seconds)
    set_channel(ch, False)


def sweep():
    for ch in relays:
        set_channel(ch, True)
        time.sleep(0.3)
        set_channel(ch, False)


def interactive():
    print(f"Bench-test {len(relays)}-ch ({', '.join(f'R{c}=BCM{p}' for c, p in zip(relays, PINS))})")
    print("1-8 = toggle daný kanál, a = all OFF, A = all ON, s = status, q = quit")
    print(f"Start: {status()}")
    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if cmd in ("q", "quit", "exit"):
            break
        elif cmd.isdigit():
            ch = int(cmd)
            if ch in relays:
                set_channel(ch, not relays[ch].value)
            else:
                print(f"jen 1-{len(relays)}")
        elif cmd == "a":
            all_channels(False)
        elif cmd == "A":
            all_channels(True)
        elif cmd == "s":
            print(status())
        elif cmd:
            print(f"1-{len(relays)} = toggle, a/A = all OFF/ON, s = status, q = quit")


def hold(label):
    print(f"Drží {label}. Ctrl+C ukončí (a vrátí všechny do OFF).")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()


def main():
    try:
        args = sys.argv[1:]
        if not args:
            interactive()
            return
        action = args[0].lower()
        if action == "all" and len(args) >= 2:
            on = args[1].lower() == "on"
            all_channels(on)
            if on:
                hold("all ON")
        elif action == "on" and len(args) >= 2:
            set_channel(int(args[1]), True)
            hold(f"R{args[1]} ON")
        elif action == "off" and len(args) >= 2:
            set_channel(int(args[1]), False)
        elif action == "pulse" and len(args) >= 2:
            dur = float(args[2]) if len(args) >= 3 else 0.5
            pulse(int(args[1]), dur)
        elif action == "sweep":
            sweep()
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
