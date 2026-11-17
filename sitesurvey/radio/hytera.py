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
        self._last_lp: dict | None = None
        # Se useremo l'RSSI dal Location Protocol, attiviamo i report con RSSI.
        if self.cfg.get("rssi", {}).get("method") == "lp" and self._radio_id():
            try:
                link.configure_rssi_reports(self._radio_id(), int(self.cfg.get("cai_network", 10)))
            except Exception as e:  # noqa: BLE001
                log.warning("config report RSSI LP fallita: %s", e)

    def _radio_id(self) -> int:
        try:
            return int(self.cfg.get("radio_id", 0) or 0)
        except (TypeError, ValueError):
            return 0

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
        rssi = self.cfg.get("rssi", {})
        method = rssi.get("method", "none")
        scale = float(rssi.get("scale", 1.0))
        offset = float(rssi.get("offset", 0.0))
        if method == "lp":
            # RSSI dal Location Protocol (report 0xA004/0xD004): GPS+RSSI insieme.
            rep = self._query_lp()
            if rep and rep.get("rssi_raw") is not None:
                return -abs(rep["rssi_raw"] * scale + offset)
            return None
        if method == "channel_status_level":
            st = self._status()
            level = st.get("signal_level", 0) if st else 0
            return -abs(level * scale + offset) if level > 0 else None
        return None  # non tarato: onesto (il livello grezzo e' nel probe)

    def _query_lp(self) -> dict | None:
        if not self.link or not self._radio_id():
            return None
        try:
            rep = self.link.query_location(self._radio_id(), int(self.cfg.get("cai_network", 10)))
        except Exception as e:  # noqa: BLE001
            self.close()
            raise RadioError(f"LP query: {e}") from e
        if rep:
            self._last_lp = rep
        return rep

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

    # --- GPS via Location Protocol ---------------------------------------
    def get_location(self) -> GpsFix | None:
        if not self._radio_id():
            return None  # senza radio_id non si puo' interrogare l'LP: usare GPS USB
        rep = self._query_lp()
        if not rep or "lat" not in rep:
            return GpsFix(valid=False, source="radio")
        return GpsFix(lat=rep.get("lat"), lon=rep.get("lon"), speed_kmh=rep.get("speed_kmh"),
                      heading=rep.get("heading"), valid=rep.get("valid", False), source="radio")

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
