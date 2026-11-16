"""Driver Hytera (PD/MD/HP/HM ...) collegato via USB.

La radio via USB e' un'interfaccia RNDIS (radio 192.168.10.1 di default).
I servizi dati usano HRNP (Hytera Radio Network Protocol) che trasporta
messaggi HDAP: RCP (controllo radio) e LP (localizzazione).

IMPORTANTE: gli opcode RCP per l'RSSI dipendono da modello/firmware e sono
documentati solo nell'SDK Hytera (DMR ADK). Sono quindi configurabili
(radio.hytera.rssi_*). Usare `sitesurvey probe --raw` per verificarli.
"""
from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time

from ..models import GpsFix
from .base import RadioDriver, RadioError, cai_ip, hexbytes

log = logging.getLogger(__name__)

# ---- HRNP -----------------------------------------------------------------
HRNP_HEADER = 0x7E
HRNP_VERSION = 0x04
OP_DATA = 0x00
OP_DATA_ACK = 0x10
OP_CLOSE_ACK = 0xFA
OP_CLOSE = 0xFB
OP_REJECT = 0xFC
OP_ACCEPT = 0xFD
OP_CONNECT = 0xFE

# ---- HDAP -----------------------------------------------------------------
HDAP_RCP = 0x02
HDAP_LP = 0x08
HDAP_END = 0x03
LP_IMMEDIATE_REQ = 0xA001
LP_IMMEDIATE_RSP = 0xA002


def hrnp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f">{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def hrnp_build(opcode: int, pn: int, payload: bytes = b"", src: int = 0x20, dst: int = 0x10) -> bytes:
    length = 12 + len(payload)
    hdr = struct.pack(">BBBBBBHHH", HRNP_HEADER, HRNP_VERSION, 0x00, opcode, src, dst, pn & 0xFFFF, length, 0)
    cs = hrnp_checksum(hdr + payload)
    return hdr[:10] + struct.pack(">H", cs) + payload


def hrnp_parse(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < 12 or frame[0] != HRNP_HEADER:
        raise ValueError("frame HRNP non valido")
    opcode = frame[3]
    pn, length = struct.unpack(">HH", frame[6:10])
    return opcode, pn, frame[12:length]


def hdap_checksum(body: bytes) -> int:
    return ((~sum(body)) + 0x33) & 0xFF


def hdap_build(msg_hdr: int, opcode: int, data: bytes, little_endian: bool) -> bytes:
    fmt = "<HH" if little_endian else ">HH"
    body = struct.pack(fmt, opcode, len(data)) + data
    return bytes([msg_hdr]) + body + bytes([hdap_checksum(body), HDAP_END])


def hdap_parse(msg: bytes, little_endian: bool) -> tuple[int, int, bytes]:
    if len(msg) < 7 or msg[-1] != HDAP_END:
        raise ValueError("messaggio HDAP non valido")
    fmt = "<HH" if little_endian else ">HH"
    opcode, n = struct.unpack(fmt, msg[1:5])
    return msg[0] & 0x7F, opcode, msg[5:5 + n]


_GPS_RE = re.compile(rb"(\d{6})(?:\.\d+)?([AV])(\d{4}\.\d+)([NS])(\d{5}\.\d+)([EW])(\d+\.?\d*)?")


def parse_lp_gps(data: bytes) -> GpsFix:
    """Estrae la stringa GPS (formato tipo NMEA concatenato) dalla risposta LP."""
    m = _GPS_RE.search(data)
    if not m:
        return GpsFix(valid=False, source="radio")
    lat = int(m.group(3)[:2]) + float(m.group(3)[2:]) / 60
    lon = int(m.group(5)[:3]) + float(m.group(5)[3:]) / 60
    if m.group(4) == b"S":
        lat = -lat
    if m.group(6) == b"W":
        lon = -lon
    speed = float(m.group(7)) * 1.852 if m.group(7) else None  # nodi -> km/h
    return GpsFix(lat=lat, lon=lon, speed_kmh=speed, valid=m.group(2) == b"A", source="radio")


class HrnpLink:
    """Collegamento HRNP verso un servizio della radio (una porta per servizio)."""

    def __init__(self, host: str, port: int, transport: str, timeout: float):
        self.host, self.port, self.transport, self.timeout = host, port, transport, timeout
        self.sock: socket.socket | None = None
        self.pn = 0
        self.lock = threading.Lock()

    def _send(self, frame: bytes) -> None:
        if self.transport == "tcp":
            self.sock.sendall(frame)
        else:
            self.sock.sendto(frame, (self.host, self.port))

    def _recv(self) -> bytes:
        if self.transport == "tcp":
            hdr = self._recv_exact(12)
            (length,) = struct.unpack(">H", hdr[8:10])
            return hdr + self._recv_exact(max(0, length - 12))
        data, _ = self.sock.recvfrom(4096)
        return data

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise RadioError("HRNP: connessione chiusa")
            buf += c
        return buf

    def open(self) -> None:
        try:
            if self.transport == "tcp":
                self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            else:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(self.timeout)
            self._send(hrnp_build(OP_CONNECT, 0))
            op, _, _ = hrnp_parse(self._recv())
            if op != OP_ACCEPT:
                raise RadioError(f"HRNP {self.port}: connessione rifiutata (op 0x{op:02x})")
        except (OSError, ValueError) as e:
            self.close()
            raise RadioError(f"HRNP {self.host}:{self.port}: {e}") from e

    def close(self) -> None:
        if self.sock:
            try:
                self._send(hrnp_build(OP_CLOSE, self.pn))
            except OSError:
                pass
            self.sock.close()
        self.sock = None

    def transact(self, payload: bytes, match=None, timeout: float | None = None) -> bytes | None:
        """Invia un messaggio HDAP e attende la risposta (filtrata da match)."""
        with self.lock:
            if not self.sock:
                raise RadioError("HRNP non connesso")
            self.pn = (self.pn + 1) & 0xFFFF
            try:
                self._send(hrnp_build(OP_DATA, self.pn, payload))
                deadline = time.monotonic() + (timeout or self.timeout)
                while (left := deadline - time.monotonic()) > 0:
                    self.sock.settimeout(left)
                    op, pn, data = hrnp_parse(self._recv())
                    if op == OP_DATA:
                        self._send(hrnp_build(OP_DATA_ACK, pn))
                        if match is None or match(data):
                            return data
                    elif op in (OP_CLOSE, OP_REJECT):
                        raise RadioError("HRNP: chiuso dalla radio")
            except socket.timeout:
                return None
            except (OSError, ValueError) as e:
                raise RadioError(f"HRNP: {e}") from e
        return None


class HyteraDriver(RadioDriver):
    vendor = "hytera"

    def __init__(self, cfg: dict, mode: str = "dmr"):
        super().__init__(cfg, mode)
        self.host = cfg["host"]
        self.timeout = float(cfg.get("timeout_s", 3.0))
        self.rcp: HrnpLink | None = None
        self.lp: HrnpLink | None = None
        self.data_sock: socket.socket | None = None
        self._req = 0
        self._ok = False

    def connect(self) -> None:
        self.close()
        tr = self.cfg.get("transport", "udp")
        self.rcp = HrnpLink(self.host, int(self.cfg["rcp_port"]), tr, self.timeout)
        self.rcp.open()
        self.lp = HrnpLink(self.host, int(self.cfg["lp_port"]), tr, self.timeout)
        try:
            self.lp.open()
        except RadioError as e:
            log.warning("servizio LP non disponibile (GPS radio disattivato): %s", e)
            self.lp = None
        self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ok = True

    def close(self) -> None:
        for link in (self.rcp, self.lp):
            if link:
                link.close()
        if self.data_sock:
            self.data_sock.close()
        self.rcp = self.lp = self.data_sock = None
        self._ok = False

    def is_connected(self) -> bool:
        return self._ok

    def info(self) -> dict:
        return {"vendor": self.vendor, "mode": self.mode, "host": self.host}

    def read_rssi(self) -> float | None:
        op = int(self.cfg.get("rssi_opcode", 0))
        if not op:
            raise RadioError("radio.hytera.rssi_opcode non configurato (vedi docs/PROTOCOLLI.md)")
        req = hdap_build(HDAP_RCP, op, hexbytes(self.cfg.get("rssi_request_data", "")), True)
        reply_op = op | 0x8000

        def match(d: bytes) -> bool:
            try:
                return hdap_parse(d, True)[1] == reply_op
            except ValueError:
                return False

        data = self.rcp.transact(req, match)
        if data is None:
            self._ok = False
            raise RadioError("RCP: nessuna risposta RSSI")
        _, _, body = hdap_parse(data, True)
        off, n = int(self.cfg["rssi_reply_offset"]), int(self.cfg["rssi_reply_len"])
        raw = body[off:off + n]
        if len(raw) < n:
            return None
        v = int.from_bytes(raw, "little", signed=True)
        return -abs(v * float(self.cfg["rssi_scale"]) + float(self.cfg["rssi_offset"]))

    def get_location(self) -> GpsFix | None:
        if not self.lp:
            return None
        self._req = (self._req + 1) & 0xFFFFFFFF
        ip = socket.inet_aton(cai_ip(int(self.cfg["radio_id"]), int(self.cfg["cai_network"])))
        req = hdap_build(HDAP_LP, LP_IMMEDIATE_REQ, struct.pack(">I", self._req) + ip, False)
        data = self.lp.transact(req, lambda d: d[:1] == bytes([HDAP_LP]))
        if data is None:
            return GpsFix(valid=False, source="radio")
        return parse_lp_gps(data)

    def send_beacon(self, seq: int, b: dict) -> bool:
        kind = b.get("kind", "data")
        if kind == "data":
            dst = cai_ip(int(b["target_id"]), int(self.cfg["cai_network"]))
            try:
                self.data_sock.sendto(f"{b.get('payload', 'SITESURVEY')} {seq}".encode(),
                                      (dst, int(b["target_port"])))
                return True
            except OSError as e:
                log.warning("beacon dati verso %s fallito: %s", dst, e)
                return False
        if kind == "ptt":
            key, dekey = hexbytes(self.cfg["ptt_key_data"]), hexbytes(self.cfg["ptt_dekey_data"])
            if not key:
                raise RadioError("radio.hytera.ptt_key_data non configurato")
            op = int(self.cfg["ptt_opcode"])
            self.rcp.transact(hdap_build(HDAP_RCP, op, key, True), timeout=1.0)
            time.sleep(float(b.get("ptt_ms", 500)) / 1000)
            self.rcp.transact(hdap_build(HDAP_RCP, op, dekey, True), timeout=1.0)
            return True
        return False
