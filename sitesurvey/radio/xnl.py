"""MOTOTRBO XNL/XCMP su TCP (porta 8002 della radio via USB).

XNL e' il livello di trasporto/autenticazione, XCMP il protocollo di controllo
(stato radio, RSSI, PTT, ...). L'autenticazione usa TEA con chiave e delta
forniti da Motorola tramite il programma ADK: NON sono inclusi nel software,
vanno messi nel file di configurazione.
"""
from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
from dataclasses import dataclass

from .base import RadioError

log = logging.getLogger(__name__)

MASTER_STATUS_BRDCST = 0x0002
MASTER_PRSNT_BRDCST = 0x0003
DEV_AUTH_KEY_REQUEST = 0x0004
DEV_AUTH_KEY_REPLY = 0x0005
DEV_CONN_REQUEST = 0x0006
DEV_CONN_REPLY = 0x0007
DEV_SYSMAP_REQUEST = 0x0008
DEV_SYSMAP_BRDCST = 0x0009
DATA_MSG = 0x000B
DATA_MSG_ACK = 0x000C

PROTO_XNL = 0x00
PROTO_XCMP = 0x01

XCMP_DEVINIT_STATUS = 0xB400
XCMP_RADIO_STATUS = 0x000E

DEVICE_TYPE = 0x0A
M32 = 0xFFFFFFFF


def tea_encrypt(block: bytes, key: tuple[int, int, int, int], delta: int, rounds: int = 32) -> bytes:
    v0, v1 = struct.unpack(">II", block)
    k0, k1, k2, k3 = key
    s = 0
    for _ in range(rounds):
        s = (s + delta) & M32
        v0 = (v0 + ((((v1 << 4) & M32) + k0) ^ ((v1 + s) & M32) ^ ((v1 >> 5) + k1))) & M32
        v1 = (v1 + ((((v0 << 4) & M32) + k2) ^ ((v0 + s) & M32) ^ ((v0 >> 5) + k3))) & M32
    return struct.pack(">II", v0, v1)


@dataclass
class XnlPacket:
    opcode: int
    proto: int
    flags: int
    dst: int
    src: int
    txid: int
    payload: bytes = b""

    def encode(self) -> bytes:
        body = struct.pack(">HBBHHHH", self.opcode, self.proto, self.flags, self.dst,
                           self.src, self.txid, len(self.payload)) + self.payload
        return struct.pack(">H", len(body)) + body

    @classmethod
    def decode(cls, body: bytes) -> "XnlPacket":
        if len(body) < 12:
            raise ValueError("pacchetto XNL troppo corto")
        op, proto, flags, dst, src, txid, plen = struct.unpack(">HBBHHHH", body[:12])
        return cls(op, proto, flags, dst, src, txid, body[12:12 + plen])


class XnlClient:
    def __init__(self, host: str, port: int, key, delta: int, timeout: float = 3.0):
        self.host, self.port = host, port
        self.key = tuple(int(k) & M32 for k in key)
        self.delta = int(delta) & M32
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.master = 0
        self.addr = 0
        self.tx_base = 0
        self._tx = 0
        self._flags = 0
        self._send_lock = threading.Lock()
        self._waiters: dict[int, list[queue.Queue]] = {}
        self._waiters_lock = threading.Lock()
        self._alive = False
        self._reader: threading.Thread | None = None
        self.broadcast_handler = None  # callable(opcode, data)

    # ---- trasporto -------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RadioError("connessione XNL chiusa dalla radio")
            buf += chunk
        return buf

    def _read_packet(self) -> XnlPacket:
        (n,) = struct.unpack(">H", self._recv_exact(2))
        return XnlPacket.decode(self._recv_exact(n))

    def _send(self, pkt: XnlPacket) -> None:
        with self._send_lock:
            self.sock.sendall(pkt.encode())

    def _wait_for(self, opcode: int) -> XnlPacket:
        for _ in range(50):
            pkt = self._read_packet()
            if pkt.opcode == opcode:
                return pkt
            log.debug("XNL handshake: ignoro opcode 0x%04x", pkt.opcode)
        raise RadioError(f"XNL: opcode 0x{opcode:04x} non ricevuto")

    # ---- connessione -----------------------------------------------------
    def connect(self) -> None:
        if not any(self.key):
            raise RadioError("chiave XNL non configurata (radio.motorola.xnl_key): richiede Motorola ADK")
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            pkt = self._wait_for(MASTER_STATUS_BRDCST)
            self.master = pkt.src
            self._send(XnlPacket(DEV_AUTH_KEY_REQUEST, PROTO_XNL, 0, self.master, 0, 0))
            rep = self._wait_for(DEV_AUTH_KEY_REPLY)
            temp_addr = struct.unpack(">H", rep.payload[0:2])[0]
            seed = rep.payload[2:10]
            enc = tea_encrypt(seed, self.key, self.delta)
            req = struct.pack(">HBB", 0x0000, DEVICE_TYPE, 0x01) + enc
            self._send(XnlPacket(DEV_CONN_REQUEST, PROTO_XNL, 0, self.master, temp_addr, 0, req))
            rep = self._wait_for(DEV_CONN_REPLY)
            if not rep.payload or rep.payload[0] != 0x01:
                raise RadioError("XNL: autenticazione rifiutata (chiave/delta errati?)")
            self.tx_base = rep.payload[1]
            self.addr = struct.unpack(">H", rep.payload[2:4])[0]
        except (OSError, ValueError, struct.error) as e:
            self.close()
            raise RadioError(f"XNL connessione fallita: {e}") from e
        log.info("XNL connesso: master=0x%04x addr=0x%04x", self.master, self.addr)
        self.sock.settimeout(1.0)
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, name="xnl-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._alive = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    @property
    def alive(self) -> bool:
        return self._alive

    # ---- ricezione -------------------------------------------------------
    def _read_loop(self) -> None:
        try:
            while self._alive:
                try:
                    pkt = self._read_packet()
                except socket.timeout:
                    continue
                if pkt.opcode == DATA_MSG:
                    self._send(XnlPacket(DATA_MSG_ACK, pkt.proto, pkt.flags, pkt.src, self.addr, pkt.txid))
                    if pkt.proto == PROTO_XCMP and len(pkt.payload) >= 2:
                        self._dispatch(pkt.payload)
        except Exception as e:  # noqa: BLE001
            if self._alive:
                log.warning("XNL reader terminato: %s", e)
        finally:
            self._alive = False

    def _dispatch(self, xcmp: bytes) -> None:
        opcode = struct.unpack(">H", xcmp[:2])[0]
        data = xcmp[2:]
        if opcode == XCMP_DEVINIT_STATUS and data[4:5] == b"\x00":
            # la radio annuncia il suo stato: rispondiamo con il nostro DevInitStatus
            mine = struct.pack(">H", XCMP_DEVINIT_STATUS) + data[0:4] + bytes([0x00, DEVICE_TYPE, 0x00, 0x00, 0x00])
            self.send_xcmp(mine)
            return
        with self._waiters_lock:
            qs = self._waiters.get(opcode)
            if qs:
                qs.pop(0).put(data)
                return
        if self.broadcast_handler:
            self.broadcast_handler(opcode, data)

    # ---- invio XCMP ------------------------------------------------------
    def send_xcmp(self, xcmp: bytes) -> None:
        if not self.sock:
            raise RadioError("XNL non connesso")
        txid = ((self.tx_base << 8) | (self._tx & 0xFF)) & 0xFFFF
        self._tx += 1
        flags = self._flags
        self._flags = (self._flags + 1) & 0x07
        try:
            self._send(XnlPacket(DATA_MSG, PROTO_XCMP, flags, self.master, self.addr, txid, xcmp))
        except OSError as e:
            self._alive = False
            raise RadioError(f"invio XCMP fallito: {e}") from e

    def request(self, opcode: int, data: bytes = b"", timeout: float | None = None) -> bytes:
        q: queue.Queue = queue.Queue(1)
        reply_op = opcode | 0x8000
        with self._waiters_lock:
            self._waiters.setdefault(reply_op, []).append(q)
        try:
            self.send_xcmp(struct.pack(">H", opcode) + data)
            return q.get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise RadioError(f"XCMP 0x{opcode:04x}: nessuna risposta") from None
        finally:
            with self._waiters_lock:
                lst = self._waiters.get(reply_op, [])
                if q in lst:
                    lst.remove(q)
