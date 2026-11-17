import json
import threading
import time

from sitesurvey.config import DEFAULTS, _merge
from sitesurvey.control import ControlServer, send_command
from sitesurvey.display import status_lines
from sitesurvey.export import (export_session, grid, level, load_samples, split_roles,
                               to_geojson, to_html)
from sitesurvey.surveyor import Surveyor


def make_cfg(tmp_path):
    return _merge(DEFAULTS, {
        "general": {"data_dir": str(tmp_path), "control_socket": str(tmp_path / "ctl.sock")},
        "radio": {"driver": "simulator", "simulator": {"speed_kmh": 3000}},
        "gps": {"source": "radio"},
        "survey": {"interval_s": 0.05, "idle_interval_s": 0.05, "rssi_samples": 2,
                   "rssi_sample_spacing_ms": 1},
        "beacon": {"settle_ms": 1},
        "display": {"driver": "none", "button_gpio": -1},
    })


def test_end_to_end_simulator(tmp_path):
    cfg = make_cfg(tmp_path)
    sv = Surveyor(cfg)
    ctl = ControlServer(cfg["general"]["control_socket"], sv)
    ctl.start()
    t = threading.Thread(target=sv.run, daemon=True)
    t.start()
    try:
        time.sleep(0.3)
        st = send_command(cfg["general"]["control_socket"], "status")
        assert st["radio_connected"] and st["gps"]["valid"] and not st["recording"]
        assert send_command(cfg["general"]["control_socket"], "toggle")["recording"]
        time.sleep(1.0)
        st = send_command(cfg["general"]["control_socket"], "stop")
        assert not st["recording"] and st["samples"] > 5
        assert "error" in send_command(cfg["general"]["control_socket"], "boh")
    finally:
        sv.shutdown()
        t.join(3)
        ctl.stop()

    sess = next((tmp_path / "sessions").iterdir())
    meta = json.loads((sess / "meta.json").read_text())
    assert meta["samples"] == st["samples"]
    samples = load_samples(sess)
    assert len(samples) == st["samples"]
    files = export_session(sess, cell_m=100)
    html = files["html"].read_text()
    assert "__DATA__" not in html and '"samples":[' in html
    assert "<kml" in files["kml"].read_text()
    gj = json.loads(files["geojson"].read_text())
    assert gj["features"]


def test_level_and_grid():
    assert level(-70)[0] == "Ottimo"
    assert level(-100)[0] == "Discreto"
    assert level(-130)[0] == "Critico"
    assert level(None)[0] == "Nessun segnale"
    s = [{"lat": 45.0, "lon": 9.0, "rssi": -80}, {"lat": 45.00001, "lon": 9.00001, "rssi": -90},
         {"lat": 45.01, "lon": 9.01, "rssi": None}]
    cells = grid(s, 50)
    assert len(cells) == 2
    full = [c for c in cells if c["n"] == 2][0]
    assert full["rssi"] == -85


def test_field_radios_separate_in_export(tmp_path):
    # base (downlink) + due radio sul campo con RSSI non ancora misurato (None)
    import csv
    from sitesurvey.models import SAMPLE_FIELDS
    d = tmp_path / "s"
    d.mkdir()
    with (d / "samples.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SAMPLE_FIELDS)
        rows = [
            {"timestamp": 1, "lat": 45.0, "lon": 9.0, "rssi_dbm": -80, "role": "base", "radio_id": "100"},
            {"timestamp": 2, "lat": 45.001, "lon": 9.001, "rssi_dbm": -90, "role": "base", "radio_id": "100"},
            {"timestamp": 3, "lat": 45.02, "lon": 9.02, "rssi_dbm": "", "role": "field", "radio_id": "200"},
            {"timestamp": 4, "lat": 45.03, "lon": 9.03, "rssi_dbm": "", "role": "field", "radio_id": "201"},
        ]
        for r in rows:
            w.writerow([r.get(k, "") for k in SAMPLE_FIELDS])
    base, field = split_roles(load_samples(d))
    assert len(base) == 2 and len(field) == 2
    assert {s["radio_id"] for s in field} == {"200", "201"}
    # la griglia/copertura si calcola solo sulla base
    cells = grid(base, 50)
    assert cells and all(c["rssi"] is not None for c in cells)
    gj = to_geojson(base, cells, [-85, -95, -105, -115], field)
    roles = {ft["properties"].get("role") for ft in gj["features"] if ft["geometry"]["type"] == "Point"}
    assert roles == {"base", "field"}
    html = to_html(base, cells, [-85, -95, -105, -115], "t", field)
    assert '"field":[' in html and '"radios":["200","201"]' in html


def test_status_lines():
    snap = {"radio_connected": True, "radio": {"vendor": "motorola", "model": "DM4600", "mode": "dmr"},
            "gps": {"valid": True, "sats": 9, "source": "radio"}, "recording": True, "elapsed_s": 3725,
            "samples": 12, "last_rssi": -91.2, "message": ""}
    lines = status_lines(snap, True)
    assert lines[0].startswith("RADIO OK DM4600")
    assert lines[1].startswith("GPS FIX 9sat")
    assert lines[2].startswith("REC*01:02:05 n12")
    assert all(len(x) <= 21 for x in lines[:3])
    snap.update(radio_connected=False, recording=False, gps={"valid": False})
    lines = status_lines(snap, False)
    assert lines[0].startswith("RADIO --") and lines[1].startswith("GPS --") and lines[2].startswith("STOP")
