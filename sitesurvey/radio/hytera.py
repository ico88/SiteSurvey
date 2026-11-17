"""Driver Hytera (MD785i/MD785G e simili) via HRNP su USB bulk.

Basato su ico88/DMR-Tracker (hytera_link.py), verificato sul campo. Il controllo
passa dall'endpoint USB bulk (NON rete). Dal channel-status (0x00E7) si ottiene
frequenza, stato "occupato" (carrier) e un livello di segnale grezzo (tag 0x09):
e' il punto di partenza per il reverse dell'RSSI in dBm.

GPS via Hytera (servizio LP): formato ancora da validare sulla seconda radio,
quindi get_location() resta non supportato (si usa un GPS USB esterno).
"""
from __future__ import annotations

import logging
import threading
import time

from ..models import GpsFix
from . import hytera_usb
from .base import RadioDriver, RadioError

log = logging.getLogger(__name__)


class HyteraDriver(RadioDriver):
    vendor = "hytera"

    def __init__(self, cfg: dict, mode: str = "dmr"):
        super().__init__(cfg, mode)
        self.link: hytera_usb.HyteraUsb | None = None
        self._lock = threading.Lock()
        self._last_status: dict | None = None
        self._status_ts = 0.0

    def connect(self) -> None:
        self.close()
        ok, why = hytera_usb.usb_available()
        if not ok:
            raise RadioError(f"USB non disponibile: {why}")
        usb = self.cfg.get("usb", {})
        link = hytera_usb.HyteraUsb(
            vid=hytera_usb.HYTERA_VID,
            pid=int(usb["pid"], 0) if usb.get("pid") else None,
            serial=str(usb.get("serial", "")))
        link.open()
        link.connect()
        self.link = link

    def close(self) -> None:
        if self.link:
            try:
                self.link.close()
            except Exception:  # noqa: BLE001
                pass
        self.link = None

    def is_connected(self) -> bool:
        return self.link is not None

    def info(self) -> dict:
        return {"vendor": self.vendor, "mode": self.mode, "transport": "usb"}

    def _status(self, max_age: float = 0.0) -> dict | None:
        if not self.link:
            raise RadioError("radio non connessa")
        if max_age and self._last_status and (time.time() - self._status_ts) < max_age:
            return self._last_status
        try:
            st = self.link.query_channel_status()
        except Exception as e:  # noqa: BLE001
            self.close()
            raise RadioError(f"channel-status: {e}") from e
        if st:
            self._last_status = st
            self._status_ts = time.time()
        return st

    # --- RSSI -------------------------------------------------------------
    def read_rssi(self) -> float | None:
        st = self._status()
        if not st:
            return None
        rssi = self.cfg.get("rssi", {})
        level = st.get("signal_level", 0)
        if rssi.get("method") == "channel_status_level":
            # Mappatura lineare livello grezzo -> dBm, DA TARARE su hardware.
            scale = float(rssi.get("scale", 1.0))
            offset = float(rssi.get("offset", 0.0))
            if level <= 0:
                return None if st.get("busy") else None
            return -abs(level * scale + offset)
        # Senza taratura restituiamo None per onesta': il livello grezzo e'
        # comunque esposto nel campo diagnostico (vedi probe).
        return None

    def signal_level(self) -> int:
        st = self._status(max_age=0.3)
        return int(st.get("signal_level", 0)) if st else 0

    def is_busy(self) -> bool:
        st = self._status(max_age=0.3)
        return bool(st and st.get("busy"))

    def rssi_probe_frames(self) -> list[str]:
        """Interroga gli opcode candidati e restituisce le risposte grezze (hex)."""
        out = []
        if not self.link:
            return out
        for op in hytera_usb.RCP_RSSI_CANDIDATES:
            try:
                r = self.link.transact(hytera_usb.build_hdap(
                    hytera_usb.HDAP_SERVICE["RCP"], op, b""))
                out.append(f"0x{op:04x}: {r.hex() if r else '(nessuna risposta)'}")
            except Exception as e:  # noqa: BLE001
                out.append(f"0x{op:04x}: errore {e}")
        return out

    # --- GPS (non ancora validato) ---------------------------------------
    def get_location(self) -> GpsFix | None:
        return None  # GPS via Hytera LP: formato da validare; usare GPS USB

    # --- Beacon -----------------------------------------------------------
    def send_beacon(self, seq: int, b: dict) -> bool:
        kind = b.get("kind", "data")
        if kind in ("ptt", "data"):
            # Via USB il percorso dati RNDIS non e' garantito: usiamo un PTT breve
            # per generare portante e far leggere il carrier/livello.
            if not self.link:
                raise RadioError("radio non connessa")
            try:
                self.link.ptt(True)
                time.sleep(float(b.get("ptt_ms", 500)) / 1000)
                self.link.ptt(False)
                return True
            except Exception as e:  # noqa: BLE001
                self.close()
                raise RadioError(f"beacon PTT: {e}") from e
        return False
