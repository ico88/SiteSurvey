"""Ciclo principale: connessione radio, GPS, beacon, lettura RSSI, registrazione."""
from __future__ import annotations

import logging
import threading
import time

from .gps import NmeaGps
from .models import GpsFix, Sample, haversine_m
from .radio import RadioError, make_driver
from .radio.base import aggregate
from .recorder import SessionRecorder

log = logging.getLogger(__name__)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.radio_connected = False
        self.radio_info: dict = {}
        self.gps = GpsFix(valid=False, timestamp=0)
        self.recording = False
        self.session_id = ""
        self.session_start = 0.0
        self.samples = 0
        self.last_rssi: float | None = None
        self.message = ""

    def snapshot(self) -> dict:
        with self.lock:
            g = self.gps
            return {
                "radio_connected": self.radio_connected,
                "radio": dict(self.radio_info),
                "gps": {"valid": g.valid, "lat": g.lat, "lon": g.lon, "sats": g.sats,
                        "source": g.source, "age_s": round(g.age(), 1) if g.timestamp else None},
                "recording": self.recording,
                "session": self.session_id,
                "elapsed_s": round(time.time() - self.session_start) if self.recording else 0,
                "samples": self.samples,
                "last_rssi": self.last_rssi,
                "message": self.message,
            }


class Surveyor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = State()
        self.radio = make_driver(cfg)
        self.nmea: NmeaGps | None = None
        if cfg["gps"]["source"] in ("nmea", "auto"):
            self.nmea = NmeaGps(cfg["gps"]["nmea_device"], int(cfg["gps"]["nmea_baud"]))
        self.recorder: SessionRecorder | None = None
        self._rec_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_connect_try = 0.0
        self._last_pos: tuple[float, float] | None = None
        self._seq = 0
        self._clock_set = False

    # ---- comandi ---------------------------------------------------------
    def start_recording(self) -> dict:
        with self._rec_lock:
            if not self.recorder:
                meta = {"radio": self.state.snapshot()["radio"], "config": {
                    k: self.cfg[k] for k in ("beacon", "survey")}, "driver": self.cfg["radio"]["driver"],
                    "mode": self.cfg["radio"]["mode"]}
                self.recorder = SessionRecorder(self.cfg["general"]["data_dir"], meta,
                                                int(self.cfg["survey"]["fsync_every"]))
                self._last_pos = None
                with self.state.lock:
                    self.state.recording = True
                    self.state.session_id = self.recorder.dir.name
                    self.state.session_start = self.recorder.started
                    self.state.samples = 0
                log.info("registrazione avviata: %s", self.recorder.dir)
        return self.state.snapshot()

    def stop_recording(self) -> dict:
        with self._rec_lock:
            if self.recorder:
                self.recorder.close()
                log.info("registrazione fermata: %s (%d campioni)", self.recorder.dir, self.recorder.count)
                self.recorder = None
                with self.state.lock:
                    self.state.recording = False
        return self.state.snapshot()

    def toggle_recording(self) -> dict:
        return self.stop_recording() if self.recorder else self.start_recording()

    def shutdown(self) -> None:
        self._stop.set()

    # ---- ciclo -----------------------------------------------------------
    def run(self) -> None:
        if self.nmea:
            self.nmea.start()
        if self.cfg["survey"]["auto_start"]:
            self.start_recording()
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                self._ensure_radio()
                fix = self._update_gps()
                if self.recorder and self.state.radio_connected:
                    self._take_sample(fix)
                iv = float(self.cfg["survey"]["interval_s"] if self.recorder
                           else self.cfg["survey"]["idle_interval_s"])
                self._stop.wait(max(0.05, iv - (time.monotonic() - t0)))
        finally:
            self.stop_recording()
            self.radio.close()
            if self.nmea:
                self.nmea.stop()

    def _set_msg(self, msg: str) -> None:
        with self.state.lock:
            self.state.message = msg

    def _radio_lost(self, err: Exception) -> None:
        log.warning("radio: %s", err)
        self.radio.close()
        with self.state.lock:
            self.state.radio_connected = False
            self.state.message = str(err)[:60]

    def _ensure_radio(self) -> None:
        if self.radio.is_connected():
            return
        if self.state.radio_connected:
            self._radio_lost(RadioError("connessione persa"))
        if time.monotonic() - self._last_connect_try < float(self.cfg["radio"]["reconnect_interval_s"]):
            return
        self._last_connect_try = time.monotonic()
        try:
            self.radio.connect()
        except (RadioError, OSError) as e:
            self._set_msg(str(e)[:60])
            log.debug("connessione radio fallita: %s", e)
            return
        info = self.radio.info()
        log.info("radio connessa: %s", info)
        with self.state.lock:
            self.state.radio_connected = True
            self.state.radio_info = info
            self.state.message = ""

    def _update_gps(self) -> GpsFix:
        gcfg = self.cfg["gps"]
        max_age = float(gcfg["max_age_s"])
        fix = None
        if self.nmea:
            f = self.nmea.fix()
            if f.is_fresh(max_age):
                fix = f
        if fix is None and gcfg["source"] in ("radio", "auto") and self.state.radio_connected:
            try:
                fix = self.radio.get_location()
            except RadioError as e:
                self._radio_lost(e)
        if fix is not None and (fix.valid or not self.state.gps.is_fresh(max_age)):
            with self.state.lock:
                self.state.gps = fix
        if fix and fix.valid and fix.gps_time and gcfg.get("set_system_time") and not self._clock_set:
            self._maybe_set_clock(fix.gps_time)
        with self.state.lock:
            return self.state.gps

    def _maybe_set_clock(self, gps_time: float) -> None:
        if abs(time.time() - gps_time) > 2:
            try:
                time.clock_settime(time.CLOCK_REALTIME, gps_time)
                log.info("orologio di sistema impostato da GPS")
            except (PermissionError, OSError) as e:
                log.warning("impossibile impostare l'orologio: %s", e)
        self._clock_set = True

    def _take_sample(self, fix: GpsFix) -> None:
        sc, bc = self.cfg["survey"], self.cfg["beacon"]
        valid = fix.is_fresh(float(self.cfg["gps"]["max_age_s"]))
        if sc["require_gps_fix"] and not valid:
            self._set_msg("attesa fix GPS")
            return
        min_d = float(sc["min_distance_m"])
        if min_d > 0 and valid and self._last_pos:
            if haversine_m(*self._last_pos, fix.lat, fix.lon) < min_d:
                return
        self._seq += 1
        beacon_ok = False
        values: list[float] = []
        try:
            if bc["enabled"] and bc["kind"] != "none":
                beacon_ok = self.radio.send_beacon(self._seq, bc)
                self._stop.wait(float(bc["settle_ms"]) / 1000)
            for i in range(max(1, int(sc["rssi_samples"]))):
                v = self.radio.read_rssi()
                if v is not None:
                    values.append(v)
                if i + 1 < int(sc["rssi_samples"]):
                    self._stop.wait(float(sc["rssi_sample_spacing_ms"]) / 1000)
        except RadioError as e:
            self._radio_lost(e)
            return
        rssi = aggregate(values, sc["rssi_aggregate"])
        base_id = str(self.cfg["radio"].get(self.cfg["radio"]["driver"], {}).get("base_radio_id", "") or "")
        s = self._make_sample(fix, valid, rssi, values, beacon_ok, radio_id=base_id, role="base")
        rows = [s]
        # Radio sul campo (topologia "entrambe insieme"): un campione per radio
        # con fix fresco. L'RSSI uplink per-radio e' un reverse a parte: None.
        if sc.get("track_field_radios", False) and hasattr(self.radio, "field_locations"):
            try:
                for rid, ffix in self.radio.field_locations(float(self.cfg["gps"]["max_age_s"])).items():
                    rows.append(self._make_sample(ffix, True, None, [], beacon_ok,
                                                   radio_id=rid, role="field"))
            except RadioError as e:
                self._radio_lost(e)
                return
        with self._rec_lock:
            if not self.recorder:
                return
            for row in rows:
                self.recorder.write(row)
            n = self.recorder.count
        if valid:
            self._last_pos = (fix.lat, fix.lon)
        with self.state.lock:
            self.state.samples = n
            self.state.last_rssi = rssi
            self.state.message = ""

    def _make_sample(self, fix, valid, rssi, values, beacon_ok, radio_id="", role="base") -> Sample:
        return Sample(
            timestamp=time.time(), gps_time=fix.gps_time,
            lat=fix.lat if valid else None, lon=fix.lon if valid else None,
            alt=fix.alt, speed_kmh=fix.speed_kmh, heading=fix.heading,
            gps_valid=valid, sats=fix.sats, hdop=fix.hdop, gps_source=fix.source,
            rssi_dbm=rssi, rssi_min=min(values) if values else None,
            rssi_max=max(values) if values else None, rssi_n=len(values),
            beacon_seq=self._seq, beacon_ok=beacon_ok,
            mode=self.cfg["radio"]["mode"], vendor=self.radio.vendor,
            radio_id=radio_id, role=role)
