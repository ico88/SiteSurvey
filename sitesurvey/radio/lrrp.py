"""MOTOTRBO LRRP (Location Request/Response Protocol), codifica binaria a token.

Usato per leggere il GPS interno della radio collegata via USB.
Riferimento: decoder pubblici LRRP (es. DSD-FME), MOTOTRBO System Planner.
"""
from __future__ import annotations

import calendar
import struct

IMMEDIATE_REQ = 0x05
IMMEDIATE_RSP = 0x07
TRIGGERED_START_REQ = 0x09
TRIGGERED_START_RSP = 0x0B
TRIGGERED_REPORT = 0x0D
TRIGGERED_STOP_REQ = 0x0F
TRIGGERED_STOP_RSP = 0x11

TOK_REQUEST_ID = 0x22
TOK_RESULT = 0x37
TOK_RESULT_ALT = 0x38
TOK_INFO_TIME = 0x34
TOK_POINT_2D = 0x66
TOK_POINT_3D = 0x69
TOK_CIRCLE_2D = 0x51
TOK_CIRCLE_3D = 0x54
TOK_SPEED_HOR = 0x6C
TOK_DIRECTION_HOR = 0x56

RESULT_OK = 0x00


def encode_uintvar(v: int) -> bytes:
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


def decode_uintvar(buf: bytes, i: int) -> tuple[int, int]:
    v = 0
    while True:
        b = buf[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i


def _wrap(msg_type: int, body: bytes) -> bytes:
    return bytes([msg_type]) + encode_uintvar(len(body)) + body


def _req_id(request_id: int) -> bytes:
    rid = struct.pack(">I", request_id & 0xFFFFFFFF)
    return bytes([TOK_REQUEST_ID, len(rid)]) + rid


def build_immediate_request(request_id: int) -> bytes:
    # 0x51 0x40 = ret-info con lat/long/time richiesti
    return _wrap(IMMEDIATE_REQ, _req_id(request_id) + bytes([0x51, 0x40]))


def build_triggered_request(request_id: int, interval_s: int) -> bytes:
    body = _req_id(request_id) + bytes([0x51, 0x40, 0x34, 0x31]) + encode_uintvar(interval_s)
    return _wrap(TRIGGERED_START_REQ, body)


def encode_lat(lat: float) -> bytes:
    raw = int(round(abs(lat) * (1 << 31) / 90.0)) & 0x7FFFFFFF
    if lat < 0:
        raw |= 0x80000000
    return struct.pack(">I", raw)


def encode_lon(lon: float) -> bytes:
    return struct.pack(">i", int(round(lon * (1 << 31) / 180.0)))


def decode_lat(b: bytes) -> float:
    raw = struct.unpack(">I", b)[0]
    lat = (raw & 0x7FFFFFFF) * 90.0 / (1 << 31)
    return -lat if raw & 0x80000000 else lat


def decode_lon(b: bytes) -> float:
    return struct.unpack(">i", b)[0] * 180.0 / (1 << 31)


def encode_time(epoch: float) -> bytes:
    import time as _t
    t = _t.gmtime(epoch)
    v = (t.tm_year << 26) | (t.tm_mon << 22) | (t.tm_mday << 17) | (t.tm_hour << 12) | (t.tm_min << 6) | t.tm_sec
    return v.to_bytes(5, "big")


def decode_time(b: bytes) -> float | None:
    v = int.from_bytes(b, "big")
    year, mon, day = v >> 26, (v >> 22) & 0xF, (v >> 17) & 0x1F
    hh, mm, ss = (v >> 12) & 0x1F, (v >> 6) & 0x3F, v & 0x3F
    try:
        return float(calendar.timegm((year, mon, day, hh, mm, ss, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def build_point_response(request_id: int, lat: float, lon: float, alt: int | None = None,
                         epoch: float | None = None, result: int = RESULT_OK) -> bytes:
    """Costruisce una risposta (usato da simulatore e test)."""
    body = _req_id(request_id)
    if result != RESULT_OK:
        body += bytes([TOK_RESULT]) + encode_uintvar(result)
        return _wrap(IMMEDIATE_RSP, body)
    if epoch is not None:
        body += bytes([TOK_INFO_TIME]) + encode_time(epoch)
    if alt is None:
        body += bytes([TOK_POINT_2D]) + encode_lat(lat) + encode_lon(lon)
    else:
        body += bytes([TOK_POINT_3D]) + encode_lat(lat) + encode_lon(lon) + encode_uintvar(max(0, alt))
    return _wrap(IMMEDIATE_RSP, body)


def parse_message(data: bytes) -> dict:
    """Decodifica una risposta/report LRRP. Restituisce un dict con i campi trovati."""
    if len(data) < 2:
        raise ValueError("pacchetto LRRP troppo corto")
    out: dict = {"type": data[0]}
    length, i = decode_uintvar(data, 1)
    body = data[i:i + length]
    j = 0
    try:
        while j < len(body):
            tok = body[j]
            j += 1
            if tok == TOK_REQUEST_ID:
                n = body[j]
                out["request_id"] = int.from_bytes(body[j + 1:j + 1 + n], "big")
                j += 1 + n
            elif tok in (TOK_RESULT, TOK_RESULT_ALT):
                out["result"], j = decode_uintvar(body, j)
            elif tok == TOK_INFO_TIME:
                out["time"] = decode_time(body[j:j + 5])
                j += 5
            elif tok in (TOK_POINT_2D, TOK_POINT_3D, TOK_CIRCLE_2D, TOK_CIRCLE_3D):
                out["lat"] = decode_lat(body[j:j + 4])
                out["lon"] = decode_lon(body[j + 4:j + 8])
                j += 8
                if tok in (TOK_CIRCLE_2D, TOK_CIRCLE_3D):
                    out["radius"], j = decode_uintvar(body, j)
                if tok in (TOK_POINT_3D, TOK_CIRCLE_3D):
                    out["alt"], j = decode_uintvar(body, j)
            elif tok == TOK_SPEED_HOR:
                ip, j = decode_uintvar(body, j)
                out["speed_kmh"] = ip * 3.6  # m/s -> km/h (parte intera)
                break  # ufloatvar: parte frazionaria non standardizzata, stop qui
            elif tok == TOK_DIRECTION_HOR:
                out["heading"] = body[j] * 2.0
                j += 1
            else:
                break  # token sconosciuto: ci fermiamo, i dati gia' letti restano validi
    except IndexError:
        pass
    out.setdefault("result", RESULT_OK if "lat" in out else None)
    return out
