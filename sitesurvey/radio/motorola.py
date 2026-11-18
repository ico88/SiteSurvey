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
from .base import RadioDriver, RadioError, cai_ip, rid_from_cai_ip
from .xcmp import XcmpSession

log = logging.getLogger(__name__)


BASE_ID = "base"


class LrrpListener(threading.Thread):
    """Ascolta UDP 4001 e tiene il GPS PER radio (base + radio sul campo).

    I report della radio collegata arrivano dall'host locale (192.168.10.1) e
    sono etichettati 'base'. I report via etere arrivano dall'IP CAI della radio
    sul campo (es. 12.x.y.z): il Radio ID si ricava dall'IP (rid_from_cai_ip),
    cosi' ogni radio resta separata anche se ce ne sono molte.
    """

    def __init__(self, host: str, port: int, local_port: int, request_template: str = ""):
        super().__init__(name="lrrp", daemon=True)
        self.host, self.port, self.local_port = host, port, local_port
        self.request_template = request_template
        self._lock = threading.Lock()
        self._fixes: dict[str, GpsFix] = {}   # radio_id ("base"/"<id>") -> fix
        self.sock: socket.socket | None = None
        self._stop = threading.Event()
        self._seq = 0x38

    def _key_for(self, ip: str) -> str:
        if ip == self.host:
            return BASE_ID
        rid = rid_from_cai_ip(ip)
        return str(rid) if rid is not None else ip

    def base_fix(self) -> GpsFix | None:
        with self._lock:
            f = self._fixes.get(BASE_ID)
            return GpsFix(**vars(f)) if f else None

    def field_fixes(self, max_age_s: float) -> dict[str, GpsFix]:
        with self._lock:
            return {rid: GpsFix(**vars(f)) for rid, f in self._fixes.items()
                    if rid != BASE_ID and f.is_fresh(max_age_s)}

    def request(self, target_ip: str | None = None) -> None:
        if not self.sock:
            return
        self._seq = (self._seq + 0x09) & 0xFF
        try:
            self.sock.sendto(lrrp.build_immediate_request(self._seq, self.request_template),
                             (target_ip or self.host, self.port))
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
            key = self._key_for(addr[0])
            with self._lock:
                self._fixes[key] = GpsFix(
                    lat=msg["lat"], lon=msg["lon"], speed_kmh=msg.get("speed_kmh"),
                    heading=msg.get("heading"), valid=True, source="radio",
                    gps_time=msg.get("time"))
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
        self._rssi_menu_entered = False

    def connect(self) -> None:
        self.close()
        self.lrrp = LrrpListener(self.host, int(self.cfg["lrrp_port"]),
                                 int(self.cfg["lrrp_local_port"]),
                                 request_template=self.cfg.get("lrrp_request_template", ""))
        self.lrrp.start()
        if self.cfg.get("ars_enabled", True):
            self.ars = ArsListener(int(self.cfg.get("ars_port", 4005)))
            self.ars.start()
        self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._rssi_menu_entered = False
        # XCMP e' opzionale: serve per beacon PTT e per la lettura RSSI.
        rssi = self.cfg.get("rssi", {})
        need_xcmp = (rssi.get("method") in ("radiostatus", "xcmp_notify", "disptxt_menu")
                     or self.cfg.get("xcmp_enabled", False))
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
        """GPS della radio base collegata. Richiesta attiva, salvo passivo-only."""
        if not self.lrrp:
            raise RadioError("radio non connessa")
        if not self.cfg.get("lrrp_passive_only", False):
            self.lrrp.request()
        fix = self.lrrp.base_fix()
        return fix if fix else GpsFix(valid=False, source="radio")

    def field_locations(self, max_age_s: float = 10.0) -> dict[str, GpsFix]:
        """GPS delle radio sul campo (via LRRP etere), per Radio ID.

        Se `poll_field_radios` e' attivo e l'ARS conosce delle radio online, le
        interroga; in ogni caso restituisce i fix freschi gia' ricevuti.
        """
        if not self.lrrp:
            return {}
        if self.cfg.get("poll_field_radios", False) and self.ars:
            net = int(self.cfg["cai_network"])
            for r in self.ars.online_radios():
                try:
                    rid = int(r["radio_id"])
                except (ValueError, KeyError):
                    continue
                try:
                    self.lrrp.request(cai_ip(rid, net))
                except (RadioError, ValueError):
                    pass
        return self.lrrp.field_fixes(max_age_s)

    # --- RSSI --------------------------------------------------------------
    def read_rssi(self) -> float | None:
        rssi = self.cfg.get("rssi", {})
        method = rssi.get("method", "none")
        if not (self.xcmp and self.xcmp.alive):
            return None
        if method == "radiostatus":
            # Metodo del MOTOTRBO Site Survey ufficiale: RadioStatusRequest con
            # condizione RSSI (0x02), reply decodificato come -(b0 + b1/256).
            self.xcmp.request_radio_status()
            time.sleep(float(rssi.get("reply_wait_s", 0.25)))
            return self.xcmp.read_rssi_radiostatus()
        if method == "disptxt_menu":
            # Metodo noto (trboxcmp, MIT): entra nel menu RSSI nascosto una volta,
            # poi leggi il dBm dal testo del display che la radio trasmette.
            if not self._rssi_menu_entered:
                try:
                    self.xcmp.enter_rssi_menu(rssi.get("menu_sequence") or None)
                    self._rssi_menu_entered = True
                    time.sleep(0.3)
                except (RuntimeError, ValueError) as e:
                    log.warning("ingresso menu RSSI fallito: %s", e)
                    return None
            return self.xcmp.read_rssi_from_display(rssi.get("text_regex", ""))
        if method == "xcmp_notify":
            subop = int(rssi.get("notify_subop", 0xB40E))
            off = int(rssi.get("value_offset", 0))
            n = int(rssi.get("value_len", 1))
            signed = bool(rssi.get("signed", True))
            scale = float(rssi.get("scale", 1.0))
            offset = float(rssi.get("offset", 0.0))
            for frame in reversed(self.xcmp.notifications):
                pos = frame.find(subop.to_bytes(2, "big"))
                if pos < 0:
                    continue
                raw = frame[pos + 2 + off:pos + 2 + off + n]
                if len(raw) < n:
                    continue
                return -abs(int.from_bytes(raw, "big", signed=signed) * scale + offset)
        return None

    def rssi_probe_frames(self) -> list[str]:
        """Frame XCMP grezzi + testo del display, per diagnosticare l'RSSI."""
        if not self.xcmp:
            return []
        out = [f"display: {t}" for t in self.xcmp.display_texts()[-5:]]
        out += [f"frame: {f.hex()}" for f in self.xcmp.notifications[-5:]]
        return out

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
