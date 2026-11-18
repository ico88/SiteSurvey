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
import re
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

# --- RSSI "ufficiale": XCMP Radio Status (come il MOTOTRBO Site Survey) -------
# Costanti e formula ricavate per interoperabilita' dal tool ufficiale Motorola
# (decompilazione di Mototrbo.Xcmp.dll, posseduto dall'utente). NON contiene
# chiavi/segreti: sono fatti di protocollo.
RADIO_STATUS_REQ = 0x000E
RADIO_STATUS_REPLY = 0x800E
STATUS_COND_RSSI = 0x02        # StatusCondition.ReadRssi
STATUS_COND_RADIO_ID = 0x0E


def decode_rssi(value: bytes) -> float | None:
    """RSSI in dBm dal valore del RadioStatusReply: -(b0 + b1/256)."""
    if len(value) < 2:
        return None
    return -(value[0] + value[1] / 256.0)

# --- Lettura RSSI Motorola: non c'e' uno status XCMP pulito. Il metodo noto
# (progetto MIT nr23730/trboxcmp) e': entrare nel menu RSSI nascosto della radio
# con una sequenza di tasti, poi leggere il numero dal testo del display, che la
# radio trasmette via broadcast DISPTXT. Opcode da trboxcmp (MIT):
DISPTXT_BCAST = 0xB401       # broadcast testo del display
PINPUT_BCAST = 0xB405        # invio di un tasto fisico
# Codici tasti (sottoinsieme)
BTN = {"left": 0x80, "right": 0x82, "up": 0x87, "down": 0x88, "menu": 0x8B,
       "back": 0x81, "ok": 0x55, "p1": 0xA0, "p2": 0xA1}
# Sequenza per il menu RSSI nascosto: sinistra x3, destra x3
RSSI_MENU_SEQUENCE = ["left", "left", "left", "right", "right", "right"]
# Riconoscimento di un dBm nel testo del display (es. "-95", "RSSI -101dBm")
_DBM_RE = re.compile(r"-?\d{2,3}")


def tea_encrypt_be(block8: bytes, key16: bytes) -> bytes:
    """TEA classico (32 round, big-endian) usato dall'auth XNL MOTOTRBO.

    Risposta al challenge = TEA(random8, chiave16). Con la chiave corretta
    l'autenticazione riesce ad OGNI tentativo (non serve la lookup).
    """
    if len(block8) != 8 or len(key16) != 16:
        raise ValueError("TEA: challenge 8 byte e chiave 16 byte")
    v0, v1 = struct.unpack(">2I", block8)
    k0, k1, k2, k3 = struct.unpack(">4I", key16)
    total = 0
    delta = 0x9E3779B9
    mask = 0xFFFFFFFF
    for _ in range(32):
        total = (total + delta) & mask
        v0 = (v0 + (((v1 << 4) + k0) ^ (v1 + total) ^ ((v1 >> 5) + k1))) & mask
        v1 = (v1 + (((v0 << 4) + k2) ^ (v0 + total) ^ ((v0 >> 5) + k3))) & mask
    return struct.pack(">2I", v0, v1)


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
                 timeout: float = 5.0, max_attempts: int = 300, key: bytes | None = None):
        self.host, self.port, self.timeout = host, port, timeout
        self.lookup = {**DEFAULT_LOOKUP, **(lookup or {})}
        self.key = key  # chiave XNL (16 byte): se presente, auth TEA ad ogni tentativo
        # con la chiave bastano pochi tentativi; la lookup ne richiede molti
        self.max_attempts = max_attempts if not key else max(5, min(max_attempts, 10))
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
                magic, random8 = body[0:2], body[2:10]
                response = None
                if self.key:
                    response = tea_encrypt_be(random8, self.key)   # auth nativa
                elif random8.hex() in self.lookup:
                    response = bytes.fromhex(self.lookup[random8.hex()])  # fallback lookup
                if response is not None:
                    s.sendall(pack_ext(0x0006, b"\x00\x00" + magic,
                                       bytes.fromhex("00000a01") + response))
                    state = "WAIT_AUTH"
                else:
                    break  # senza chiave e random non in tabella: chiudi e ritenta
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

    def _send_data_cmd(self, subop: int, payload: bytes) -> None:
        """Invia un comando DATA XCMP (framing verificato in DMR-Tracker)."""
        if not self.sock:
            raise RuntimeError("XCMP non connesso")
        body_len = 2 + 1 + len(payload)
        inner = (struct.pack(">H", 0x000b) + bytes([0x01, 0x00]) + struct.pack(">H", 0x0006)
                 + struct.pack(">H", 0x0002) + struct.pack(">H", 0x0000)
                 + struct.pack(">H", body_len) + struct.pack(">H", subop)
                 + bytes([len(payload)]) + payload)
        with self._lock:
            self.sock.sendall(struct.pack(">H", len(inner)) + inner)

    def send_ptt(self, on: bool, ms: int = 500) -> None:
        self._send_data_cmd(PTT_SUBOP, PTT_ON if on else PTT_OFF)

    # --- RSSI via Radio Status (metodo del tool ufficiale) ----------------
    def request_radio_status(self, condition: int = STATUS_COND_RSSI) -> None:
        """Invia un XCMP RadioStatusRequest (0x000E) con la condizione data.

        Il trasporto riusa il framing provato per il PTT; il formato esatto puo'
        variare per modello: verificabile con Wireshark sul tool ufficiale.
        """
        self._send_data_cmd(RADIO_STATUS_REQ, bytes([condition & 0xFF]))

    def read_rssi_radiostatus(self) -> float | None:
        """Cerca l'ultimo RadioStatusReply RSSI e lo converte in dBm.

        Cerca la sequenza [80 0e] e, poco dopo, la condizione 02 seguita dai due
        byte di valore. Formula ufficiale: -(b0 + b1/256).
        """
        marker = bytes([0x80, 0x0E])
        for frame in reversed(self.notifications):
            pos = frame.find(marker)
            if pos < 0:
                continue
            tail = frame[pos + 2:pos + 10]
            # la condizione 0x02 compare subito dopo un eventuale byte di result
            for off in (0, 1):
                if len(tail) >= off + 3 and tail[off] == STATUS_COND_RSSI:
                    val = decode_rssi(tail[off + 1:off + 3])
                    if val is not None and -130 <= val <= -20:
                        return val
        return None

    # --- lettura RSSI dal menu nascosto + display -------------------------
    def press_button(self, name: str, hold_ms: int = 100) -> None:
        """Simula la pressione di un tasto fisico (PINPUT). name in BTN."""
        code = BTN.get(name)
        if code is None:
            raise ValueError(f"tasto sconosciuto: {name}")
        # payload PINPUT: 00 00 00 + tasto + stato + 00 03 (da trboxcmp, MIT)
        self._send_data_cmd(PINPUT_BCAST, bytes([0x00, 0x00, 0x00, code, 0x01, 0x00, 0x03]))
        time.sleep(hold_ms / 1000)
        self._send_data_cmd(PINPUT_BCAST, bytes([0x00, 0x00, 0x00, code, 0x00, 0x00, 0x03]))

    def enter_rssi_menu(self, sequence=None) -> None:
        """Naviga al menu RSSI nascosto (default: sinistra x3, destra x3)."""
        for b in (sequence or RSSI_MENU_SEQUENCE):
            self.press_button(b)
            time.sleep(0.15)

    def display_texts(self) -> list[str]:
        """Estrae il testo dai broadcast DISPTXT raccolti."""
        out = []
        marker = DISPTXT_BCAST.to_bytes(2, "big")
        for frame in self.notifications:
            if marker in frame:
                # testo = run ASCII/latin1 stampabili nel frame
                txt = "".join(chr(b) if 32 <= b < 127 else " " for b in frame)
                out.append(" ".join(txt.split()))
        return out

    def read_rssi_from_display(self, regex: str = "") -> float | None:
        """Cerca un dBm nel testo del display (dopo enter_rssi_menu)."""
        rx = re.compile(regex) if regex else _DBM_RE
        for txt in reversed(self.display_texts()):
            m = rx.search(txt)
            if m:
                try:
                    v = float(m.group(1) if m.groups() else m.group(0))
                except (ValueError, IndexError):
                    continue
                if -130 <= v <= -20:
                    return v
        return None
