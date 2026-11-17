"""Transport di controllo Hytera: HRNP su USB bulk (pyusb).

Portato da ico88/DMR-Tracker (hytera_link.py / hytera_test), **verificato sul
campo** con MD785i. La radio mobile parla HRNP direttamente sull'endpoint USB
bulk 0x04(OUT)/0x84(IN) — NON IP/RNDIS/UDP (quello e' il percorso dei
ripetitori). Handshake CONNECT(0xFE)->ACCEPT(0xFD), poi DATA(0x00) con dentro
HDAP/RCP.

pyusb e' importato in modo protetto: il modulo resta importabile anche senza
pyusb (la disponibilita' si verifica con usb_available()).
"""
from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

HYTERA_VID = 0x238B
HYTERA_PID = 0x0A21
EP_OUT = 0x04
EP_IN = 0x84
HRNP_HEADER = 0x7E

HDAP_SERVICE = {"RCP": 0x02, "LP": 0x08, "TMP": 0x09, "RRS": 0x11}
RCP_OPCODE = {
    "button": 0x0041,
    "call-request": 0x0841,
    "channel-status": 0x00E7,   # ChannelStatusOrParameterCheck -> freq, busy, livello
    "zone-channel": 0x00C4,
    "radio-check": 0x0833,
}
# Opcode candidati per il reverse dell'RSSI numerico (da validare su hardware).
RCP_RSSI_CANDIDATES = [0x00E7, 0x0227, 0x0201]
BUTTON_TARGET = {"front_ptt": 0x03, "back_ptt": 0x1E, "channel_up": 0x22, "channel_down": 0x23}
BUTTON_OP = {"release": 0, "press": 1, "short": 2, "long": 3}

# Location Protocol (servizio LP = 0x08). Report con RSSI incluso.
# (struttura verificata sulla libreria OK-DMR/ok-dmrlib, AGPL: qui riscritta.)
LP_STANDARD_REQUEST = 0xA001
LP_STANDARD_REPORT = 0xA002
LP_STANDARD_REPORT_RSSI = 0xA004
LP_CONDITION_REPORT = 0xD003
LP_CONDITION_REPORT_RSSI = 0xD004
LP_RSSI_CONFIG_REQUEST = 0xE001
LP_RSSI_CONFIG_ANSWER = 0xE002
HYTERA_CAI_NET = 10

# GPSData LP: 40 byte, campi ASCII tipo-NMEA.
_LP_GPS_RE = re.compile(rb"([AV])(\d{6})(\d{6})([NS])(\d{4}\.\d+)([EW])(\d{5}\.\d+)(\d+\.?\d*)?(\d+)?")


def cai_ip_bytes(radio_id: int, net: int = HYTERA_CAI_NET) -> bytes:
    return bytes([net, (radio_id >> 16) & 0xFF, (radio_id >> 8) & 0xFF, radio_id & 0xFF])


def build_lp_request(radio_id: int, net: int = HYTERA_CAI_NET, request_id: int = 1) -> bytes:
    """LP StandardRequest (0xA001): payload = request_id(4 BE) + radio_ip(4)."""
    payload = struct.pack(">I", request_id & 0xFFFFFFFF) + cai_ip_bytes(radio_id, net)
    return build_hdap(HDAP_SERVICE["LP"], LP_STANDARD_REQUEST, payload, reliable=True)


def build_lp_rssi_config(radio_id: int, net: int = HYTERA_CAI_NET, request_id: int = 1,
                         enable: bool = True) -> bytes:
    """LP RSSIReportConfigRequest (0xE001): attiva i report con RSSI."""
    payload = (struct.pack(">I", request_id & 0xFFFFFFFF) + cai_ip_bytes(radio_id, net)
               + bytes([0x01 if enable else 0x00]))
    return build_hdap(HDAP_SERVICE["LP"], LP_RSSI_CONFIG_REQUEST, payload, reliable=True)


def parse_lp_gpsdata(block: bytes) -> dict:
    """Decodifica il blocco GPSData (ASCII tipo-NMEA) dei report LP."""
    m = _LP_GPS_RE.search(block)
    if not m:
        return {"valid": False}
    lat = int(m.group(5)[:2]) + float(m.group(5)[2:]) / 60
    lon = int(m.group(7)[:3]) + float(m.group(7)[3:]) / 60
    if m.group(4) == b"S":
        lat = -lat
    if m.group(6) == b"W":
        lon = -lon
    speed = float(m.group(8)) * 1.852 if m.group(8) else None  # nodi -> km/h
    heading = float(m.group(9)) if m.group(9) else None
    return {"valid": m.group(1) == b"A", "lat": round(lat, 6), "lon": round(lon, 6),
            "speed_kmh": round(speed, 1) if speed is not None else None, "heading": heading}


def parse_lp_report(reply: bytes) -> dict | None:
    """Decodifica un report LP (0xA002/0xA004/0xD003/0xD004).

    HDAP = [service][opcode:2 BE][len:2 BE][payload][csum][0x03].
    payload = request_id(4) + radio_ip(4) + result(2) + GPSData(40) [+ RSSI].
    Il report ...RSSI (0xA004/0xD004) porta l'RSSI dopo il blocco GPS.
    """
    h = reply[12:] if len(reply) > 12 and reply[0] == HRNP_HEADER else reply
    if len(h) < 5 or (h[0] & 0x7F) != HDAP_SERVICE["LP"]:
        return None
    opcode = int.from_bytes(h[1:3], "big")
    n = int.from_bytes(h[3:5], "big")
    payload = h[5:5 + n]
    if len(payload) < 10:
        return None
    radio_ip = payload[4:8]
    rid = (radio_ip[1] << 16) | (radio_ip[2] << 8) | radio_ip[3]
    gps = parse_lp_gpsdata(payload[10:50])
    out = {"opcode": opcode, "radio_id": rid, **gps, "rssi_raw": None}
    if opcode in (LP_STANDARD_REPORT_RSSI, LP_CONDITION_REPORT_RSSI):
        tail = payload[50:]
        if tail:
            # RSSI Hytera: 1-2 byte dopo il GPS. Valore grezzo (da mappare in dBm).
            out["rssi_raw"] = int.from_bytes(tail[:2] if len(tail) >= 2 else tail[:1],
                                             "little", signed=True)
    return out

# Opcode HRNP
OP_DATA = 0x00
OP_ACCEPT = 0xFD
OP_CONNECT = 0xFE


def usb_available() -> tuple[bool, str]:
    try:
        import usb.core  # noqa: F401
        import usb.util  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return (False, f"pyusb non disponibile: {e}")
    try:
        import usb.core
        usb.core.find()
    except Exception as e:  # noqa: BLE001
        if e.__class__.__name__ == "NoBackendError":
            return (False, "backend libusb non disponibile (installare libusb-1.0)")
    return (True, "")


def _hdap_checksum(body: bytes) -> int:
    return ((~sum(body)) + 0x33) & 0xFF


def build_hdap(service: int, opcode_value: int, payload: bytes, reliable: bool = False) -> bytes:
    st = service | (0x80 if reliable else 0)
    endian = "little" if (service & 0x7F) == HDAP_SERVICE["RCP"] else "big"
    body = opcode_value.to_bytes(2, endian) + len(payload).to_bytes(2, endian) + payload
    return bytes([st]) + body + bytes([_hdap_checksum(body), 0x03])


def build_rcp_button(target: int, operation: int, reliable: bool = True) -> bytes:
    return build_hdap(HDAP_SERVICE["RCP"], RCP_OPCODE["button"],
                      struct.pack("<BB", target & 0xFF, operation & 0xFF), reliable)


def _inet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_hrnp(opcode: int, version: int, block: int, src: int, dst: int,
               pkt_num: int, payload: bytes = b"") -> bytes:
    total = 12 + len(payload)
    hdr = bytearray(12)
    hdr[0] = 0x7E
    hdr[1] = version & 0xFF
    hdr[2] = block & 0xFF
    hdr[3] = opcode & 0xFF
    hdr[4] = src & 0xFF
    hdr[5] = dst & 0xFF
    hdr[6:8] = struct.pack(">H", pkt_num & 0xFFFF)
    hdr[8:10] = struct.pack(">H", total & 0xFFFF)
    hdr[10:12] = b"\x00\x00"
    csum = _inet_checksum(bytes(hdr) + payload)
    hdr[10:12] = struct.pack(">H", csum)
    return bytes(hdr) + payload


def parse_channel_status(reply: bytes) -> dict | None:
    """Decodifica la risposta 0x00E7. Ritorna freq RX/TX, busy, livello segnale.

    Payload = 7 byte header + TLV [tag][4B LE]. Tag noti (verificati sul campo):
      0x04 RX(Hz)  0x05 TX(Hz)  0x09 livello segnale  0x0a/0x11 busy/carrier
    Il livello 0x09 e' grezzo (non dBm): e' il punto di partenza per il reverse.
    """
    if not reply or len(reply) < 18:
        return None
    h = reply[12:]
    if h[0] != 0x02 or (h[1] | (h[2] << 8)) != 0x80E7:
        return None
    pl = h[5:5 + (h[3] | (h[4] << 8))]
    header = pl[:7]
    rx = tx = None
    tags: dict[int, int] = {}
    i = 7
    while i + 5 <= len(pl):
        tag = pl[i]
        val = int.from_bytes(pl[i + 1:i + 5], "little")
        tags[tag] = val
        if tag == 0x04:
            rx = val
        elif tag == 0x05:
            tx = val
        i += 5
    if rx is None and not tags:
        return None
    return {
        "rx_hz": rx, "tx_hz": tx,
        "zone": header[1] if len(header) > 1 else None,
        "channel": header[2] if len(header) > 2 else None,
        "busy": bool(tags.get(0x0A)) or bool(tags.get(0x11)),
        "signal_level": tags.get(0x09, 0),
        "header": header.hex(),
        "tags": {f"0x{k:02x}": v for k, v in tags.items()},
    }


class HyteraUsb:
    """Sessione HRNP su USB bulk. Thread-safe tramite un lock interno."""

    def __init__(self, vid: int = HYTERA_VID, pid: int | None = None, serial: str = ""):
        self.vid = vid or HYTERA_VID
        self.pid = pid
        self.serial = serial or ""
        self.dev = None
        self._pkt = 0
        self._lock = threading.Lock()

    # ---- apertura / chiusura --------------------------------------------
    def open(self) -> None:
        import usb.core
        import usb.util
        kwargs = {"idVendor": self.vid}
        if self.pid:
            kwargs["idProduct"] = self.pid
        candidates = list(usb.core.find(find_all=True, **kwargs))
        if self.serial:
            dev = None
            for d in candidates:
                try:
                    if usb.util.get_string(d, d.iSerialNumber) == self.serial:
                        dev = d
                        break
                except Exception:  # noqa: BLE001
                    continue
        else:
            dev = candidates[0] if candidates else None
        if dev is None:
            raise RuntimeError("radio Hytera USB non trovata (VID/PID/serial)")
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:  # noqa: BLE001
            pass
        dev.set_configuration()
        self.dev = dev

    def close(self) -> None:
        if self.dev is not None:
            try:
                import usb.util
                usb.util.dispose_resources(self.dev)
            except Exception:  # noqa: BLE001
                pass
        self.dev = None

    # ---- I/O bulk --------------------------------------------------------
    def _write(self, data: bytes) -> None:
        self.dev.write(EP_OUT, data, timeout=1000)

    def _read(self, timeout: int = 1000) -> bytes:
        return bytes(self.dev.read(EP_IN, 512, timeout=timeout))

    def _next_pkt(self) -> int:
        self._pkt = (self._pkt + 1) & 0xFFFF
        return self._pkt

    # ---- sessione --------------------------------------------------------
    def connect(self) -> None:
        if self.dev is None:
            raise RuntimeError("device non aperto")
        self._write(build_hrnp(OP_CONNECT, 0x04, 0x00, 0x20, 0x10, 0))
        reply = self._read(2000)
        if len(reply) < 4 or reply[3] != OP_ACCEPT:
            raise RuntimeError("handshake HRNP non accettato dalla radio")

    def transact(self, hdap: bytes, timeout: int = 1000) -> bytes | None:
        with self._lock:
            self._write(build_hrnp(OP_DATA, 0x04, 0x00, 0x20, 0x10, self._next_pkt(), hdap))
            try:
                return self._read(timeout)
            except Exception as e:  # noqa: BLE001
                log.debug("transact: nessuna risposta (%s)", e)
                return None

    def query_channel_status(self) -> dict | None:
        reply = self.transact(build_hdap(HDAP_SERVICE["RCP"], RCP_OPCODE["channel-status"], b""))
        return parse_channel_status(reply) if reply else None

    def configure_rssi_reports(self, radio_id: int, net: int = HYTERA_CAI_NET, enable: bool = True) -> bool:
        """Attiva i report LP con RSSI (0xE001). Da chiamare una volta dopo connect."""
        reply = self.transact(build_lp_rssi_config(radio_id, net, enable=enable))
        return reply is not None

    def query_location(self, radio_id: int, net: int = HYTERA_CAI_NET, request_id: int = 1) -> dict | None:
        """Richiede la posizione (LP StandardRequest) e decodifica il report (GPS + RSSI)."""
        reply = self.transact(build_lp_request(radio_id, net, request_id), timeout=1500)
        return parse_lp_report(reply) if reply else None

    def ptt(self, on: bool) -> None:
        op = BUTTON_OP["press"] if on else BUTTON_OP["release"]
        self.transact(build_rcp_button(BUTTON_TARGET["front_ptt"], op))

    def keepalive(self) -> None:
        self.transact(build_hdap(HDAP_SERVICE["RCP"], RCP_OPCODE["channel-status"], b""), timeout=500)
