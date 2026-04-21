"""Czech eObčanka detection over a PC/SC contact reader.

Triggers relay on card insertion if the card identifies as eObčanka (SELECT
card-management AID returns 9000). No PIN, no age-from-cert — the cert on the
2021+ cards sits behind PACE/secure-messaging that we don't implement here.
"""
import logging
import threading
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

try:
    from smartcard.CardMonitoring import CardMonitor
    from smartcard.System import readers as list_readers
    _PYSCARD_OK = True
except ImportError as exc:
    logger.info(f"pyscard not available ({exc}); card reader disabled")
    CardMonitor = None
    list_readers = lambda: []
    _PYSCARD_OK = False

# Card-management AID from ParalelniPolis/obcanka-public Card.java — fingerprint
# for Czech eObčanka. SELECT succeeds with 9000 iff this is a Czech eID card.
CARD_MGMT_AID = bytes.fromhex("D20310010001000202")


class EObcankaReader:
    def __init__(self, on_event: Callable[[], None] = None):
        self.on_event = on_event or (lambda: None)
        self._state_lock = threading.Lock()
        self._card_lock = threading.Lock()
        self._card = None
        self._state = {
            "reader_present": False,
            "card_present": False,
            "is_eobcanka": False,
            "last_event_at": None,
            "error": None,
        }
        self._monitor = None

    @property
    def available(self) -> bool:
        return _PYSCARD_OK

    def start(self) -> bool:
        if not self.available:
            logger.warning("pyscard missing; card reader disabled")
            return False
        try:
            self._refresh_readers()
            self._monitor = CardMonitor()
            self._monitor.addObserver(self)
            logger.info("Card monitor started")
            return True
        except Exception as exc:
            logger.warning(f"Card monitor init failed: {exc}")
            return False

    def _refresh_readers(self):
        try:
            present = bool(list_readers())
        except Exception:
            present = False
        with self._state_lock:
            self._state["reader_present"] = present

    def update(self, observable, actions):
        """CardObserver protocol — called by pyscard monitor thread."""
        added, removed = actions
        for card in added:
            self._handle_insert(card)
        for _ in removed:
            self._handle_remove()

    def _handle_insert(self, card):
        now = datetime.now().isoformat(timespec="seconds")
        with self._state_lock:
            self._card = card
            self._state.update({
                "card_present": True,
                "is_eobcanka": False,
                "error": None,
                "last_event_at": now,
            })
        try:
            with self._card_lock:
                is_eop = self._is_eobcanka_locked()
            with self._state_lock:
                self._state["is_eobcanka"] = is_eop
            logger.info("Card inserted — eObčanka=%s", is_eop)
        except Exception as exc:
            logger.warning(f"Card AID check failed: {exc}")
            with self._state_lock:
                self._state["error"] = str(exc)[:120]
        self.on_event()

    def _handle_remove(self):
        now = datetime.now().isoformat(timespec="seconds")
        with self._state_lock:
            self._card = None
            self._state.update({
                "card_present": False,
                "is_eobcanka": False,
                "error": None,
                "last_event_at": now,
            })
        logger.info("Card removed")
        self.on_event()

    def _open_connection(self):
        with self._state_lock:
            card = self._card
        if card is None:
            raise RuntimeError("no_card")
        conn = card.createConnection()
        conn.connect()
        return conn

    def _is_eobcanka_locked(self) -> bool:
        try:
            conn = self._open_connection()
        except RuntimeError:
            return False
        try:
            apdu = [0x00, 0xA4, 0x04, 0x0C, len(CARD_MGMT_AID)] + list(CARD_MGMT_AID)
            _, sw1, sw2 = conn.transmit(apdu)
            return (sw1, sw2) == (0x90, 0x00)
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass

    def get_state(self) -> dict:
        self._refresh_readers()
        with self._state_lock:
            return dict(self._state)
