from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import GpsFix


class RadioError(Exception):
    """Errore di comunicazione con la radio (provoca riconnessione)."""


class RadioDriver(ABC):
    vendor = "generic"

    def __init__(self, cfg: dict, mode: str = "dmr"):
        self.cfg = cfg
        self.mode = mode

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def read_rssi(self) -> float | None:
        """RSSI istantaneo in dBm, None se non disponibile."""

    def get_location(self) -> GpsFix | None:
        """Posizione dal GPS interno della radio, None se non supportato."""
        return None

    def send_beacon(self, seq: int, beacon_cfg: dict) -> bool:
        """Trasmette un beacon (dato DMR o PTT breve). True se inviato."""
        return False

    def info(self) -> dict:
        return {"vendor": self.vendor, "mode": self.mode}


def cai_ip(radio_id: int, network: int) -> str:
    """Indirizzo IP 'CAI' di una radio/gruppo: <network>.<id 24 bit>.

    MOTOTRBO: individuali 12.x.x.x, gruppi 225.x.x.x (default di CPS).
    Hytera:   individuali 10.x.x.x (default).
    """
    if not 0 <= radio_id <= 0xFFFFFF:
        raise ValueError("radio_id fuori range (24 bit)")
    return f"{network}.{(radio_id >> 16) & 0xFF}.{(radio_id >> 8) & 0xFF}.{radio_id & 0xFF}"


def hexbytes(s: str) -> bytes:
    s = (s or "").replace(" ", "").replace(":", "")
    return bytes.fromhex(s) if s else b""


def aggregate(values: list[float], how: str) -> float | None:
    if not values:
        return None
    if how == "mean":
        return sum(values) / len(values)
    if how == "median":
        v = sorted(values)
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    return max(values)
