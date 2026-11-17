"""MOTOTRBO LRRP (Location Request/Response Protocol).

Parser **verificato sul campo** con Motorola DM4601e, portato dai repository
ico88/raspradio e ico88/DMR-Tracker. Usato per leggere il GPS della radio
collegata via USB (rete RNDIS 192.168.10.x), sia su richiesta (immediate) sia
in ascolto passivo dei report periodici che la radio invia su UDP 4001.

Token verificati (DM4601e):
  report 0x0D   TriggeredLocationReport
  0x34 (5B)     timestamp            0x1F (4B) current-ts
  0x55 (8B)     posizione: lat(4)+lon(4), NO altitudine
  0x6C (2B BE)  velocita' orizzontale, unita' 1/100 m/s
  0x56 (1B)     rotta, scala 0.125 gradi/bit (DA CONFERMARE: 1 byte a 0.125
                arriva solo a ~32 gradi; su hardware verificare se e' invece
                360/256 per bit. Il valore grezzo e' comunque conservato.)
  0x23 (2B)     request-id (ignorato: il radio_id arriva dall'IP via ARS)

Nota sulla scala: le radio emettono lat/lon come intero a 32 bit. Nei dati
verificati (emisfero nord/est) l'interpretazione senza segno con scala
180/2^32 e quella con segno (90/2^31) coincidono. Qui si usa quella con
segno, cosi' il parser e' corretto anche per latitudini/longitudini negative.
"""
from __future__ import annotations

import calendar
import struct

# Tipi di messaggio
IMMEDIATE_REQ = 0x05
IMMEDIATE_RSP = 0x07
TRIGGERED_START_REQ = 0x09
TRIGGERED_REPORT = 0x0D

# Token verificati sul campo
TOK_TIMESTAMP = 0x34        # 5 byte
TOK_CURRENT_TS = 0x1F       # 4 byte
TOK_REQUEST_ID = 0x23       # 2 byte (nella forma verificata)
TOK_LOC_2D = 0x55           # 8 byte: lat(4)+lon(4)
TOK_SPEED_HOR = 0x6C        # 2 byte BE, 1/100 m/s
TOK_DIRECTION_HOR = 0x56    # 1 byte, 0.125 gradi/bit
# Varianti note da altre firmware (gestite in modo tollerante)
TOK_POINT_2D = 0x66
TOK_POINT_3D = 0x69

RESULT_OK = 0x00

# Scale con segno (coerenti coi dati field-verified per coordinate positive)
_LAT_SCALE = 90.0 / (1 << 31)
_LON_SCALE = 180.0 / (1 << 31)

# Richiesta "immediate location" usata in DMR-Tracker (seq ASCII variabile)
_REQ_HEAD = bytes.fromhex("05092202")
_REQ_TAIL = bytes.fromhex("5154625764")


def build_immediate_request(seq: int = 0x38, template: str = "") -> bytes:
    """Richiesta immediata di posizione.

    Default: formato DMR-Tracker/raspradio (`seq` = contatore 1 byte in ASCII-hex).
    Se `template` e' fornito (hex catturato con Wireshark da un programma che gia'
    funziona), lo si usa tale e quale, sostituendo l'eventuale segnaposto `{seq}`
    con il contatore in ASCII-hex. Cosi' il formato esatto si cambia da config,
    senza toccare il codice.
    """
    if template:
        t = template.strip().replace(" ", "").replace(":", "")
        if "{seq}" in template:
            t = template.strip().replace(" ", "").replace(":", "").replace(
                "{seq}", f"{seq & 0xFF:02x}")
        return bytes.fromhex(t)
    seq_ascii = f"{seq & 0xFF:02x}".encode("ascii")
    return _REQ_HEAD + seq_ascii + _REQ_TAIL


def decode_lat(b: bytes) -> float:
    return round(struct.unpack(">i", b)[0] * _LAT_SCALE, 6)


def decode_lon(b: bytes) -> float:
    return round(struct.unpack(">i", b)[0] * _LON_SCALE, 6)


def encode_lat(lat: float) -> bytes:
    return struct.pack(">i", int(round(lat / _LAT_SCALE)))


def encode_lon(lon: float) -> bytes:
    return struct.pack(">i", int(round(lon / _LON_SCALE)))


def encode_time(epoch: float) -> bytes:
    t = __import__("time").gmtime(epoch)
    v = ((t.tm_year << 26) | (t.tm_mon << 22) | (t.tm_mday << 17)
         | (t.tm_hour << 12) | (t.tm_min << 6) | t.tm_sec)
    return v.to_bytes(5, "big")


def decode_time(b: bytes) -> float | None:
    v = int.from_bytes(b, "big")
    year, mon, day = v >> 26, (v >> 22) & 0xF, (v >> 17) & 0x1F
    hh, mm, ss = (v >> 12) & 0x1F, (v >> 6) & 0x3F, v & 0x3F
    try:
        return float(calendar.timegm((year, mon, day, hh, mm, ss, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def parse_message(data: bytes) -> dict:
    """Decodifica un report/risposta LRRP. Ritorna un dict con i campi trovati.

    Chiavi possibili: type, lat, lon, speed_kmh (km/h), heading (gradi),
    time (epoch UTC), result. Se e' un pacchetto valido ma senza coordinate,
    result resta None e 'lat'/'lon' sono assenti (fix non disponibile).
    """
    if len(data) < 2:
        raise ValueError("pacchetto LRRP troppo corto")
    out: dict = {"type": data[0]}
    i = 2  # salta [tipo][len/flags]
    n = len(data)
    while i < n:
        tok = data[i]
        i += 1
        if tok == TOK_TIMESTAMP and i + 5 <= n:
            out["time"] = decode_time(data[i:i + 5])
            i += 5
        elif tok == TOK_CURRENT_TS and i + 4 <= n:
            i += 4
        elif tok == TOK_REQUEST_ID and i + 2 <= n:
            out["request_id"] = int.from_bytes(data[i:i + 2], "big")
            i += 2
        elif tok in (TOK_LOC_2D, TOK_POINT_2D) and i + 8 <= n:
            out["lat"] = decode_lat(data[i:i + 4])
            out["lon"] = decode_lon(data[i + 4:i + 8])
            i += 8
        elif tok == TOK_POINT_3D and i + 8 <= n:
            out["lat"] = decode_lat(data[i:i + 4])
            out["lon"] = decode_lon(data[i + 4:i + 8])
            i += 8
            # altitudine uintvar: la saltiamo in modo tollerante
            while i < n and data[i] & 0x80:
                i += 1
            if i < n:
                i += 1
        elif tok == TOK_SPEED_HOR and i + 2 <= n:
            out["speed_kmh"] = round(struct.unpack(">H", data[i:i + 2])[0] / 100.0 * 3.6, 1)
            i += 2
        elif tok == TOK_DIRECTION_HOR and i + 1 <= n:
            out["heading"] = round(data[i] * 0.125, 1)
            i += 1
        else:
            i += 1  # token sconosciuto: avanza di 1 (tollerante, come field parser)
    if "lat" in out and "lon" in out:
        out.setdefault("result", RESULT_OK)
    else:
        out.setdefault("result", None)
    return out


def build_triggered_report(lat: float, lon: float, epoch: float | None = None,
                           speed_ms: float | None = None, heading: float | None = None) -> bytes:
    """Costruisce un report 0x0D (per simulatore e test), nel formato verificato."""
    body = bytearray([TRIGGERED_REPORT, 0x00])
    if epoch is not None:
        body += bytes([TOK_TIMESTAMP]) + encode_time(epoch)
    body += bytes([TOK_LOC_2D]) + encode_lat(lat) + encode_lon(lon)
    if speed_ms is not None:
        body += bytes([TOK_SPEED_HOR]) + struct.pack(">H", int(round(speed_ms * 100)) & 0xFFFF)
    if heading is not None:
        body += bytes([TOK_DIRECTION_HOR, int(round(heading / 0.125)) & 0xFF])
    return bytes(body)
