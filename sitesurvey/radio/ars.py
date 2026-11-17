"""MOTOTRBO ARS (Automatic Registration Service), UDP 4005.

Portato da ico88/raspradio (verificato sul campo). Ogni radio invia un
pacchetto ARS all'accensione, al cambio canale e al cambio sito. Serve a:
 - sapere quali radio sono "in aria" (per un survey con piu' radio mobili);
 - mappare IP sorgente -> Radio ID, cosi' i report LRRP passivi (che non
   contengono l'ID in chiaro) si attribuiscono alla radio giusta.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger(__name__)

ARS_PORT = 4005
_ARS_HEADER = b"\x00\x0b\xf0\x20\x06"  # header fisso verificato sul campo


def parse_ars(data: bytes, src_ip: str) -> dict | None:
    if len(data) < 8 or data[:5] != _ARS_HEADER:
        return None
    try:
        null_pos = data.index(0x00, 5)
    except ValueError:
        return None
    try:
        radio_id = data[5:null_pos].decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not (radio_id.isdigit() and 4 <= len(radio_id) <= 10):
        return None
    return {"radio_id": radio_id, "src_ip": src_ip, "online": True, "ts": time.time()}


class ArsListener(threading.Thread):
    """Ascolta ARS su UDP 4005 e mantiene la mappa IP->radio_id."""

    def __init__(self, port: int = ARS_PORT, offline_timeout: float = 300.0):
        super().__init__(name="ars", daemon=True)
        self.port = port
        self.offline_timeout = offline_timeout
        self._lock = threading.Lock()
        self.ip_to_id: dict[str, str] = {}
        self.radios: dict[str, dict] = {}
        self._stop = threading.Event()

    def radio_id_for_ip(self, ip: str) -> str | None:
        with self._lock:
            return self.ip_to_id.get(ip)

    def online_radios(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [dict(r) for r in self.radios.values()
                    if now - r["ts"] <= self.offline_timeout]

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        sock = None
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", self.port))
                sock.settimeout(1.0)
                log.info("ARS listener su UDP %d", self.port)
                while not self._stop.is_set():
                    try:
                        data, addr = sock.recvfrom(512)
                    except socket.timeout:
                        continue
                    rec = parse_ars(data, addr[0])
                    if rec:
                        with self._lock:
                            self.ip_to_id[addr[0]] = rec["radio_id"]
                            self.radios[rec["radio_id"]] = rec
            except OSError as e:
                log.warning("ARS socket: %s (riprovo)", e)
                self._stop.wait(3)
            finally:
                if sock:
                    sock.close()
                    sock = None
