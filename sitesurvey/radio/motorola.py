"""Driver MOTOTRBO (DM/DP/SL/XPR/R7 ...) collegato via USB.

La radio via USB si presenta come interfaccia di rete (CDC-ECM/RNDIS):
radio = 192.168.10.1, Raspberry = 192.168.10.x via DHCP della radio.

 - RSSI:   XCMP "Radio Status" (0x000E) condizione RSSI, su XNL (TCP 8002)
 - GPS:    LRRP immediate location request (UDP 4001) alla radio stessa
 - Beacon: pacchetto dati UDP verso l'IP CAI del destinatario (la radio lo
           trasmette in aria come dato DMR, facendo agganciare il ripetitore),
           oppure PTT breve via XCMP (modo analogico).
"""
from __future__ import annotations

import logging
import socket
import struct
import time

from ..models import GpsFix
from . import lrrp
from .base import RadioDriver, RadioError, cai_ip, hexbytes
from .xnl import XCMP_RADIO_STATUS, XnlClient

log = logging.getLogger(__name__)


def parse_key(v) -> list[int]:
    if isinstance(v, str):
        v = [x for x in v.replace(",", " ").split() if x]
    return [int(x, 0) if isinstance(x, str) else int(x) for x in v]


def decode_rssi(raw: bytes, scale: float = 1.0, offset: float = 0.0) -> float | None:
    if not raw:
        return None
    v = int.from_bytes(raw[:2], "big", signed=True) if len(raw) >= 2 else raw[0]
    dbm = v * scale + offset
    return -abs(dbm)  # un RSSI in dBm e' sempre negativo


class MotorolaDriver(RadioDriver):
    vendor = "motorola"

    def __init__(self, cfg: dict, mode: str = "dmr"):
        super().__init__(cfg, mode)
        self.host = cfg["host"]
        self.timeout = float(cfg.get("timeout_s", 3.0))
        self.xnl: XnlClient | None = None
        self.lrrp_sock: socket.socket | None = None
        self.data_sock: socket.socket | None = None
        self._req_id = int(time.time()) & 0xFFFF
        self._info: dict = {}

    def connect(self) -> None:
        self.close()
        self.xnl = XnlClient(self.host, int(self.cfg["xnl_port"]), parse_key(self.cfg["xnl_key"]),
                             int(self.cfg["xnl_delta"]), self.timeout)
        self.xnl.connect()
        try:
            self.lrrp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.lrrp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.lrrp_sock.bind(("0.0.0.0", int(self.cfg["lrrp_local_port"])))
            self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            self.close()
            raise RadioError(f"socket LRRP/dati: {e}") from e
        self._info = {"vendor": self.vendor, "mode": self.mode, "host": self.host}
        for name, code in (("model", 0x07), ("serial", 0x08), ("radio_id", 0x0E)):
            try:
                d = self.xnl.request(XCMP_RADIO_STATUS, bytes([code]), timeout=1.5)
                if d and d[0] == 0:
                    val = d[2:]
                    self._info[name] = (int.from_bytes(val, "big") if name == "radio_id"
                                        else val.split(b"\0")[0].decode("ascii", "replace"))
            except RadioError:
                pass

    def close(self) -> None:
        if self.xnl:
            self.xnl.close()
        for s in (self.lrrp_sock, self.data_sock):
            if s:
                s.close()
        self.xnl = self.lrrp_sock = self.data_sock = None

    def is_connected(self) -> bool:
        return bool(self.xnl and self.xnl.alive)

    def info(self) -> dict:
        return dict(self._info)

    # --- RSSI -------------------------------------------------------------
    def read_rssi(self) -> float | None:
        if not self.is_connected():
            raise RadioError("radio non connessa")
        d = self.xnl.request(XCMP_RADIO_STATUS, bytes([int(self.cfg["rssi_status_code"])]))
        if len(d) < 3 or d[0] != 0x00:
            return None
        return decode_rssi(d[2:], float(self.cfg["rssi_scale"]), float(self.cfg["rssi_offset"]))

    # --- GPS --------------------------------------------------------------
    def get_location(self) -> GpsFix | None:
        if not self.lrrp_sock:
            raise RadioError("radio non connessa")
        self._req_id = (self._req_id + 1) & 0xFFFFFFFF
        rid = self._req_id
        try:
            self.lrrp_sock.sendto(lrrp.build_immediate_request(rid), (self.host, int(self.cfg["lrrp_port"])))
            deadline = time.monotonic() + self.timeout
            while (left := deadline - time.monotonic()) > 0:
                self.lrrp_sock.settimeout(left)
                data, _ = self.lrrp_sock.recvfrom(1500)
                try:
                    msg = lrrp.parse_message(data)
                except ValueError:
                    continue
                if msg.get("request_id") not in (None, rid):
                    continue
                ok = msg.get("result") == lrrp.RESULT_OK and "lat" in msg
                return GpsFix(lat=msg.get("lat"), lon=msg.get("lon"), alt=msg.get("alt"),
                              speed_kmh=msg.get("speed_kmh"), heading=msg.get("heading"),
                              valid=ok, source="radio", gps_time=msg.get("time"))
        except socket.timeout:
            return GpsFix(valid=False, source="radio")
        except OSError as e:
            raise RadioError(f"LRRP: {e}") from e
        return GpsFix(valid=False, source="radio")

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
            op = int(self.cfg["ptt_opcode"])
            self.xnl.send_xcmp(struct.pack(">H", op) + hexbytes(self.cfg["ptt_key_data"]))
            time.sleep(float(b.get("ptt_ms", 500)) / 1000)
            self.xnl.send_xcmp(struct.pack(">H", op) + hexbytes(self.cfg["ptt_dekey_data"]))
            return True
        return False
