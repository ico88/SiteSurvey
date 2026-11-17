"""Driver MOTOTRBO (DM/DP/SL/XPR/R7 ...) collegato via USB.

La radio via USB e' un'interfaccia di rete (RNDIS): radio 192.168.10.1,
Raspberry via DHCP. Basato sui moduli verificati sul campo di
ico88/raspradio e ico88/DMR-Tracker.

 - GPS:    LRRP su UDP 4001. Ascolto passivo dei report periodici (0x0D) +
           richiesta immediata. Parser field-verified (vedi lrrp.py).
 - ARS:    UDP 4005, per mappare IP->radio_id e sapere chi e' in aria.
 - RSSI:   REVERSE-ENGINEERING in corso. Via XCMP (TCP 8002, auth a lookup)
           si raccolgono le notifiche (b40e "RSSI_OR_SIGNAL"); il valore
           numerico in dBm si decodifica con uno spec configurabile che va
           TARATO su hardware (vedi `sitesurvey probe --rssi-scan`).
 - Beacon: pacchetto dati UDP verso l'IP CAI del destinatario (DMR), oppure
           PTT breve via XCMP (analogico).
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from ..models import GpsFix
from . import lrrp
from .ars import ArsListener
from .base import RadioDriver, RadioError, cai_ip
from .xcmp import XcmpSession

log = logging.getLogger(__name__)


class LrrpListener(threading.Thread):
    """Ascolta UDP 4001: aggiorna l'ultimo fix e consente la richiesta attiva."""

    def __init__(self, host: str, port: int, local_port: int):
        super().__init__(name="lrrp", daemon=True)
        self.host, self.port, self.local_port = host, port, local_port
        self._lock = threading.Lock()
        self._fix: GpsFix | None = None
        self._last_ip: str | None = None
        self.sock: socket.socket | None = None
        self._stop = threading.Event()
        self._seq = 0x38

    def fix(self) -> GpsFix | None:
        with self._lock:
            return GpsFix(**vars(self._fix)) if self._fix else None

    def last_ip(self) -> str | None:
        with self._lock:
            return self._last_ip

    def request(self) -> None:
        if not self.sock:
            return
        self._seq = (self._seq + 0x09) & 0xFF
        try:
            self.sock.sendto(lrrp.build_immediate_request(self._seq), (self.host, self.port))
        except OSError as e:
            raise RadioError(f"LRRP request: {e}") from e

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", self.local_port))
            self.sock.settimeout(1.0)
        except OSError as e:
            log.error("LRRP socket %d: %s", self.local_port, e)
            return
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = lrrp.parse_message(data)
            except ValueError:
                continue
            if "lat" not in msg:
                continue  # pacchetto valido ma senza fix (radio in interno)
            with self._lock:
                self._last_ip = addr[0]
                self._fix = GpsFix(lat=msg["lat"], lon=msg["lon"],
                                   speed_kmh=msg.get("speed_kmh"), heading=msg.get("heading"),
                                   valid=True, source="radio", gps_time=msg.get("time"))
        if self.sock:
            self.sock.close()


class MotorolaDriver(RadioDriver):
    vendor = "motorola"

    def __init__(self, cfg: dict, mode: str = "dmr"):
        super().__init__(cfg, mode)
        self.host = cfg["host"]
        self.lrrp: LrrpListener | None = None
        self.ars: ArsListener | None = None
        self.xcmp: XcmpSession | None = None
        self.data_sock: socket.socket | None = None
        self._connected = False
        self._info: dict = {}

    def connect(self) -> None:
        self.close()
        self.lrrp = LrrpListener(self.host, int(self.cfg["lrrp_port"]),
                                 int(self.cfg["lrrp_local_port"]))
        self.lrrp.start()
        if self.cfg.get("ars_enabled", True):
            self.ars = ArsListener(int(self.cfg.get("ars_port", 4005)))
            self.ars.start()
        self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # XCMP e' opzionale: serve solo per beacon PTT e reverse RSSI.
        rssi = self.cfg.get("rssi", {})
        need_xcmp = rssi.get("method") == "xcmp_notify" or self.cfg.get("xcmp_enabled", False)
        if need_xcmp:
            try:
                self.xcmp = XcmpSession(self.host, int(self.cfg.get("xcmp_port", 8002)),
                                        lookup=self.cfg.get("xcmp_lookup"),
                                        max_attempts=int(self.cfg.get("xcmp_max_attempts", 300)))
                self.xcmp.connect()
            except (RuntimeError, OSError) as e:
                log.warning("XCMP non disponibile (beacon PTT / RSSI disattivati): %s", e)
                self.xcmp = None

        # Ping leggero: consideriamo "connessa" se riusciamo a bindare i socket.
        self._connected = True
        self._info = {"vendor": self.vendor, "mode": self.mode, "host": self.host,
                      "xcmp": bool(self.xcmp and self.xcmp.alive)}

    def close(self) -> None:
        for comp in (self.lrrp, self.ars):
            if comp:
                comp.stop()
        if self.xcmp:
            self.xcmp.close()
        if self.data_sock:
            self.data_sock.close()
        self.lrrp = self.ars = self.xcmp = self.data_sock = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def info(self) -> dict:
        info = dict(self._info)
        if self.ars:
            info["radios_online"] = len(self.ars.online_radios())
        return info

    # --- GPS --------------------------------------------------------------
    def get_location(self) -> GpsFix | None:
        if not self.lrrp:
            raise RadioError("radio non connessa")
        try:
            self.lrrp.request()
        except RadioError:
            raise
        fix = self.lrrp.fix()
        return fix if fix else GpsFix(valid=False, source="radio")

    # --- RSSI (reverse-engineering) --------------------------------------
    def read_rssi(self) -> float | None:
        rssi = self.cfg.get("rssi", {})
        if rssi.get("method") != "xcmp_notify" or not (self.xcmp and self.xcmp.alive):
            return None  # onesto: finche' il decode non e' tarato, nessun valore
        subop = int(rssi.get("notify_subop", 0xB40E))
        off = int(rssi.get("value_offset", 0))
        n = int(rssi.get("value_len", 1))
        signed = bool(rssi.get("signed", True))
        scale = float(rssi.get("scale", 1.0))
        offset = float(rssi.get("offset", 0.0))
        # Cerca la notifica piu' recente col sotto-opcode atteso.
        for frame in reversed(self.xcmp.notifications):
            pos = frame.find(subop.to_bytes(2, "big"))
            if pos < 0:
                continue
            raw = frame[pos + 2 + off:pos + 2 + off + n]
            if len(raw) < n:
                continue
            v = int.from_bytes(raw, "big", signed=signed)
            return -abs(v * scale + offset)
        return None

    def rssi_probe_frames(self) -> list[str]:
        """Restituisce le notifiche XCMP grezze (hex) per il reverse RSSI."""
        return [f.hex() for f in (self.xcmp.notifications if self.xcmp else [])]

    # --- Beacon -----------------------------------------------------------
    def send_beacon(self, seq: int, b: dict) -> bool:
        kind = b.get("kind", "data")
        if kind == "data":
            net = self.cfg["cai_group_network"] if b.get("target_is_group") else self.cfg["cai_network"]
            dst = cai_ip(int(b["target_id"]), int(net))
            payload = f"{b.get('payload', 'SITESURVEY')} {seq}".encode()
            try:
                self.data_sock.sendto(payload, (dst, int(b["target_port"])))
                return True
            except OSError as e:
                log.warning("beacon dati verso %s fallito: %s", dst, e)
                return False
        if kind == "ptt":
            if not (self.xcmp and self.xcmp.alive):
                raise RadioError("beacon PTT richiede XCMP (xcmp_enabled=true e lookup valida)")
            self.xcmp.send_ptt(True)
            time.sleep(float(b.get("ptt_ms", 500)) / 1000)
            self.xcmp.send_ptt(False)
            return True
        return False
