"""Radio simulata: percorso circolare attorno al sito con RSSI da modello di propagazione.

Serve per provare tutto il sistema (display, registrazione, mappe) senza radio.
"""
from __future__ import annotations

import math
import random
import time

from ..models import GpsFix
from .base import RadioDriver


class SimulatorDriver(RadioDriver):
    vendor = "simulator"

    def __init__(self, cfg: dict, mode: str = "dmr"):
        super().__init__(cfg, mode)
        self._connected = False
        self._t0 = time.time()
        self._rng = random.Random(42)

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _pos(self) -> tuple[float, float, float]:
        c = self.cfg
        r = float(c["radius_m"])
        t = time.time() - self._t0
        w = float(c["speed_kmh"]) / 3.6 / r  # rad/s
        a = w * t
        # spirale: raggio varia tra 0.1r e r per coprire il territorio
        rr = r * (0.55 + 0.45 * math.sin(a / 7))
        dn, de = rr * math.cos(a), rr * math.sin(a)
        lat = float(c["site_lat"]) + dn / 111320.0
        lon = float(c["site_lon"]) + de / (111320.0 * math.cos(math.radians(float(c["site_lat"]))))
        return lat, lon, rr

    def read_rssi(self) -> float | None:
        _, _, d = self._pos()
        d = max(d, 10.0)
        loss = 10 * float(self.cfg["path_loss_exp"]) * math.log10(d / 10.0)
        rssi = float(self.cfg["tx_dbm"]) - loss + self._rng.gauss(0, 4)
        return None if rssi < -122 else round(rssi, 1)

    def get_location(self) -> GpsFix | None:
        lat, lon, _ = self._pos()
        return GpsFix(lat=lat, lon=lon, alt=120.0, speed_kmh=float(self.cfg["speed_kmh"]),
                      sats=9, hdop=0.9, valid=True, source="radio", gps_time=time.time())

    def send_beacon(self, seq: int, beacon_cfg: dict) -> bool:
        return beacon_cfg.get("kind", "data") != "none"

    def info(self) -> dict:
        return {"vendor": self.vendor, "mode": self.mode, "model": "SIM"}
