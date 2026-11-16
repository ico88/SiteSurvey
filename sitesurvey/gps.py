"""Ricevitore GPS NMEA esterno (USB/seriale) come alternativa o backup al GPS della radio."""
from __future__ import annotations

import calendar
import logging
import threading
import time

from .models import GpsFix

log = logging.getLogger(__name__)


def nmea_checksum_ok(line: str) -> bool:
    if not line.startswith("$") or "*" not in line:
        return False
    body, cs = line[1:].split("*", 1)
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(cs[:2], 16)
    except ValueError:
        return False


def _coord(v: str, hemi: str, deg_digits: int) -> float | None:
    if not v:
        return None
    d = int(v[:deg_digits]) + float(v[deg_digits:]) / 60.0
    return -d if hemi in ("S", "W") else d


def parse_nmea(line: str) -> dict | None:
    line = line.strip()
    if not nmea_checksum_ok(line):
        return None
    f = line[1:].split("*")[0].split(",")
    kind = f[0][2:]
    try:
        if kind == "GGA":
            q = int(f[6] or 0)
            return {"type": "GGA", "lat": _coord(f[2], f[3], 2), "lon": _coord(f[4], f[5], 3),
                    "quality": q, "sats": int(f[7] or 0), "hdop": float(f[8]) if f[8] else None,
                    "alt": float(f[9]) if f[9] else None}
        if kind == "RMC":
            t = None
            if f[1] and f[9]:
                hh, mm, ss = int(f[1][0:2]), int(f[1][2:4]), int(float(f[1][4:]))
                dd, mo, yy = int(f[9][0:2]), int(f[9][2:4]), 2000 + int(f[9][4:6])
                t = float(calendar.timegm((yy, mo, dd, hh, mm, ss, 0, 0, 0)))
            return {"type": "RMC", "valid": f[2] == "A", "lat": _coord(f[3], f[4], 2),
                    "lon": _coord(f[5], f[6], 3),
                    "speed_kmh": float(f[7]) * 1.852 if f[7] else None,
                    "heading": float(f[8]) if f[8] else None, "time": t}
    except (ValueError, IndexError):
        return None
    return None


class NmeaGps(threading.Thread):
    def __init__(self, device: str, baud: int):
        super().__init__(name="nmea-gps", daemon=True)
        self.device, self.baud = device, baud
        self._lock = threading.Lock()
        self._fix = GpsFix(valid=False, source="nmea", timestamp=0)
        self._gga: dict = {}
        self._stop = threading.Event()
        self.connected = False

    def fix(self) -> GpsFix:
        with self._lock:
            return GpsFix(**vars(self._fix))

    def feed(self, line: str) -> None:
        m = parse_nmea(line)
        if not m:
            return
        with self._lock:
            if m["type"] == "GGA":
                self._gga = m
            elif m["type"] == "RMC":
                g = self._gga
                valid = m["valid"] and g.get("quality", 1) > 0
                self._fix = GpsFix(lat=m["lat"], lon=m["lon"], alt=g.get("alt"),
                                   speed_kmh=m["speed_kmh"], heading=m["heading"],
                                   sats=g.get("sats"), hdop=g.get("hdop"), valid=valid,
                                   source="nmea", gps_time=m["time"])

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            import serial  # pyserial
        except ImportError:
            log.error("pyserial non installato: GPS NMEA disabilitato")
            return
        while not self._stop.is_set():
            try:
                with serial.Serial(self.device, self.baud, timeout=1) as s:
                    self.connected = True
                    log.info("GPS NMEA aperto su %s", self.device)
                    while not self._stop.is_set():
                        raw = s.readline()
                        if raw:
                            self.feed(raw.decode("ascii", "ignore"))
            except Exception as e:  # noqa: BLE001 - porta assente, scollegata, ecc.
                if self.connected:
                    log.warning("GPS NMEA %s: %s", self.device, e)
                self.connected = False
                self._stop.wait(3)
