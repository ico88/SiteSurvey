from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass
class GpsFix:
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    speed_kmh: float | None = None
    heading: float | None = None
    sats: int | None = None
    hdop: float | None = None
    valid: bool = False
    source: str = ""
    gps_time: float | None = None  # UTC epoch fornito dal GPS (se disponibile)
    timestamp: float = field(default_factory=time.time)  # quando e' stato letto

    def age(self) -> float:
        return time.time() - self.timestamp

    def is_fresh(self, max_age_s: float) -> bool:
        return self.valid and self.age() <= max_age_s


SAMPLE_FIELDS = [
    "timestamp", "gps_time", "radio_id", "role",
    "lat", "lon", "alt", "speed_kmh", "heading",
    "gps_valid", "sats", "hdop", "gps_source",
    "rssi_dbm", "rssi_min", "rssi_max", "rssi_n",
    "beacon_seq", "beacon_ok", "mode", "vendor",
]


@dataclass
class Sample:
    timestamp: float
    gps_time: float | None
    lat: float | None
    lon: float | None
    alt: float | None
    speed_kmh: float | None
    heading: float | None
    gps_valid: bool
    sats: int | None
    hdop: float | None
    gps_source: str
    rssi_dbm: float | None
    rssi_min: float | None
    rssi_max: float | None
    rssi_n: int
    beacon_seq: int
    beacon_ok: bool
    mode: str
    vendor: str
    radio_id: str = ""      # Radio ID della radio misurata ("" = base/sconosciuto)
    role: str = "base"      # base (radio collegata) | field (radio sul campo)

    def row(self) -> list:
        out = []
        for name in SAMPLE_FIELDS:
            v = getattr(self, name)
            if v is None:
                out.append("")
            elif isinstance(v, bool):
                out.append(int(v))
            elif isinstance(v, float):
                out.append(f"{v:.7f}" if name in ("lat", "lon") else f"{v:.2f}")
            else:
                out.append(v)
        return out


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
