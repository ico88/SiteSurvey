"""MOTOTRBO XCMP su TCP 8002, con autenticazione challenge/response a lookup.

Portato da ico88/DMR-Tracker (xcmp.py). La radio genera una challenge
[magic 2B][random 8B]; il random si ripete con periodo ~170 connessioni. Per
ogni random noto si conosce la risposta corretta (estratta da catture
Wireshark). NON serve la chiave ADK/TEA: basta ritentare finche' la challenge
cade su un random in tabella.

In SiteSurvey questa sessione NON serve per il GPS (che passa da LRRP/UDP senza
auth): serve solo per il beacon via PTT e per il reverse dell'RSSI numerico
Motorola (notifiche b40e / Radio Status). La tabella LOOKUP e gli opcode sono
configurabili.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

# Tabella di default (dai dump del repo). Estendibile via config.
DEFAULT_LOOKUP = {
    "2dff77997feb0b64": "bc8d9b98126d952d",
    "3f1e8dccf7bfd90d": "69cc1659063b10d3",
    "ebff513a91373360": "78e66c348ea03e26",
}

PTT_SUBOP = 0x0415
PTT_ON = bytes.fromhex("0100")
PTT_OFF = bytes.fromhex("0200")
# Sotto-opcode noti presenti nelle notifiche (per orientarsi nel reverse RSSI)
SUB_RSSI_OR_SIGNAL = 0xB40E


def pack_ext(pkt_type: int, cookie: bytes, body: bytes) -> bytes:
    c4 = (cookie + b"\x00\x00\x00\x00")[:4]
    inner = (struct.pack(">H", pkt_type) + b"\x00\x00" + c4 + b"\x00\x00"
             + struct.pack(">H", len(body)) + body)
    return struct.pack(">H", len(inner)) + inner


def pack_std(pkt_type: int, flags: int, opcode: int, body: bytes) -> bytes:
    inner = (struct.pack(">H", pkt_type) + b"\x00" + struct.pack(">B", flags)
             + struct.pack(">HH", opcode, len(body)) + body)
    return struct.pack(">H", len(inner)) + inner


def recv_one(sock: socket.socket) -> bytes | None:
    try:
        hdr = b""
        while len(hdr) < 2:
            c = sock.recv(2 - len(hdr))
            if not c:
                return None
            hdr += c
        tl = struct.unpack(">H", hdr)[0]
        if tl == 0 or tl > 8192:
            return None
        payload = b""
        while len(payload) < tl:
            c = sock.recv(tl - len(payload))
            if not c:
                return None
            payload += c
        return hdr + payload
    except socket.timeout:
        return None


def parse_pkt(data: bytes) -> dict | None:
    if len(data) < 4:
        return None
    tl = struct.unpack(">H", data[0:2])[0]
    pt = struct.unpack(">H", data[2:4])[0]
    flg = data[5] if len(data) > 5 else 0
    EXTENDED = frozenset({0x0002, 0x0003, 0x0004, 0x0005, 0x0006, 0x0007, 0x0009})
    if pt in EXTENDED and tl >= 12:
        bl = struct.unpack(">H", data[12:14])[0] if len(data) >= 14 else 0
        body = data[14:14 + bl]
        opcode = 0
    else:
        opcode = struct.unpack(">H", data[6:8])[0] if len(data) >= 8 else 0
        bl = struct.unpack(">H", data[8:10])[0] if len(data) >= 10 else 0
        body = data[10:10 + bl]
    full_body = data[10:2 + tl]
    return {"type": pt, "flags": flg, "opcode": opcode, "body": body,
            "full_body": full_body, "raw": data[:tl + 2]}


class XcmpSession:
    """Sessione XCMP autenticata (best-effort). Gestisce keepalive in un thread."""

    def __init__(self, host: str, port: int = 8002, lookup: dict | None = None,
                 timeout: float = 5.0, max_attempts: int = 300):
        self.host, self.port, self.timeout = host, port, timeout
        self.lookup = {**DEFAULT_LOOKUP, **(lookup or {})}
        self.max_attempts = max_attempts
        self.sock: socket.socket | None = None
        self._alive = False
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self.notifications: list[bytes] = []   # frame grezzi (per il reverse RSSI)
        self._max_notif = 200

    @property
    def alive(self) -> bool:
        return self._alive

    def _one_attempt(self) -> socket.socket | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.host, self.port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            s.close()
            return None
        state = "WAIT_VI"
        vi = 0
        for _ in range(500):
            raw = recv_one(s)
            if raw is None:
                if state == "WAIT_VI":
                    continue
                break
            pkt = parse_pkt(raw)
            if not pkt:
                continue
            pt, body = pkt["type"], pkt["body"]
            if pt == 0x0002:  # version info
                vi += 1
                s.sendall(pack_ext(0x0003, b"\x00\x00\x00\x00", b""))
                if state == "WAIT_VI" and vi == 1:
                    s.sendall(pack_ext(0x0004, b"\x00\x00\x00\x00", b""))  # challenge req
                    state = "WAIT_CHAL"
                continue
            if pt == 0x0007:  # AUTH OK
                s.sendall(pack_ext(0x0009, b"\x00\x00\x00\x00",
                                   bytes.fromhex("000301010006000f010001000a01000201")))
                return s
            if pt == 0x0005 and state in ("WAIT_CHAL", "WAIT_AUTH"):
                if len(body) < 10:
                    break
                magic, random_hex = body[0:2], body[2:10].hex()
                if random_hex in self.lookup:
                    rsp = bytes.fromhex("00000a01") + bytes.fromhex(self.lookup[random_hex])
                    s.sendall(pack_ext(0x0006, b"\x00\x00" + magic, rsp))
                    state = "WAIT_AUTH"
                else:
                    break  # random non noto: chiudi e ritenta
        s.close()
        return None

    def connect(self) -> None:
        for i in range(self.max_attempts):
            s = self._one_attempt()
            if s is not None:
                self.sock = s
                self.sock.settimeout(1.0)
                self._alive = True
                self._reader = threading.Thread(target=self._read_loop, daemon=True)
                self._reader.start()
                log.info("XCMP autenticato dopo %d tentativi", i + 1)
                return
            time.sleep(0.2)
        raise RuntimeError(f"XCMP: autenticazione non riuscita in {self.max_attempts} tentativi "
                           "(random non in tabella: aggiungere coppie o aumentare i tentativi)")

    def close(self) -> None:
        self._alive = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _read_loop(self) -> None:
        while self._alive and self.sock:
            raw = recv_one(self.sock)
            if raw is None:
                if self._alive:
                    self._alive = False
                return
            pkt = parse_pkt(raw)
            if not pkt:
                continue
            if pkt["type"] == 0x000b and len(pkt["body"]) >= 2:
                sub_id = struct.unpack(">H", pkt["body"][0:2])[0]
                with self._lock:
                    try:
                        self.sock.sendall(pack_std(0x000c, pkt["flags"], 0x0002,
                                                   struct.pack(">H", sub_id) + b"\x00\x00"))
                    except OSError:
                        self._alive = False
                        return
                self.notifications.append(pkt["full_body"])
                if len(self.notifications) > self._max_notif:
                    self.notifications.pop(0)

    def send_ptt(self, on: bool, ms: int = 500) -> None:
        if not self.sock:
            raise RuntimeError("XCMP non connesso")
        payload = PTT_ON if on else PTT_OFF
        body_len = 2 + 1 + len(payload)
        inner = (struct.pack(">H", 0x000b) + bytes([0x01, 0x00]) + struct.pack(">H", 0x0006)
                 + struct.pack(">H", 0x0002) + struct.pack(">H", 0x0000)
                 + struct.pack(">H", body_len) + struct.pack(">H", PTT_SUBOP)
                 + bytes([len(payload)]) + payload)
        with self._lock:
            self.sock.sendall(struct.pack(">H", len(inner)) + inner)
