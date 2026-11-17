"""Export delle sessioni in mappa di copertura: HTML offline, KML (Google Earth), GeoJSON, griglia."""
from __future__ import annotations

import csv
import json
import math
import statistics
from importlib import resources
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_THRESHOLDS = [-85, -95, -105, -115]
# dal migliore al peggiore, piu' "nessun segnale"
LEVELS = [
    ("Ottimo", "#1a9850"),
    ("Buono", "#91cf60"),
    ("Discreto", "#fee08b"),
    ("Scarso", "#fc8d59"),
    ("Critico", "#d73027"),
]
NO_SIGNAL = ("Nessun segnale", "#000000")


def level(rssi: float | None, thresholds=DEFAULT_THRESHOLDS) -> tuple[str, str]:
    if rssi is None:
        return NO_SIGNAL
    for (name, color), t in zip(LEVELS, thresholds):
        if rssi >= t:
            return name, color
    return LEVELS[len(thresholds)] if len(thresholds) < len(LEVELS) else LEVELS[-1]


def _f(v: str):
    return float(v) if v not in ("", None) else None


def load_samples(path: str | Path) -> list[dict]:
    p = Path(path)
    if p.is_dir():
        p = p / "samples.csv"
    out = []
    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            lat, lon = _f(r.get("lat")), _f(r.get("lon"))
            if lat is None or lon is None:
                continue
            out.append({"t": _f(r["timestamp"]), "lat": lat, "lon": lon,
                        "rssi": _f(r.get("rssi_dbm")), "sats": r.get("sats") or None,
                        "seq": r.get("beacon_seq"),
                        "radio_id": r.get("radio_id") or "", "role": r.get("role") or "base"})
    return out


def split_roles(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separa i campioni della base (copertura downlink) da quelli sul campo."""
    base = [s for s in samples if s.get("role", "base") != "field"]
    field = [s for s in samples if s.get("role") == "field"]
    return base, field


def grid(samples: list[dict], cell_m: float) -> list[dict]:
    """Aggrega i campioni in celle quadrate di lato cell_m (mediana RSSI)."""
    if not samples or cell_m <= 0:
        return []
    lat0 = statistics.mean(s["lat"] for s in samples)
    dlat = cell_m / 111320.0
    dlon = cell_m / (111320.0 * math.cos(math.radians(lat0)))
    cells: dict[tuple[int, int], list] = {}
    for s in samples:
        k = (math.floor(s["lat"] / dlat), math.floor(s["lon"] / dlon))
        cells.setdefault(k, []).append(s["rssi"])
    out = []
    for (i, j), vals in cells.items():
        good = [v for v in vals if v is not None]
        out.append({"lat0": i * dlat, "lon0": j * dlon, "lat1": (i + 1) * dlat, "lon1": (j + 1) * dlon,
                    "rssi": round(statistics.median(good), 1) if good else None,
                    "n": len(vals), "no_signal": len(vals) - len(good)})
    return out


def stats(samples: list[dict], thresholds) -> dict:
    vals = [s["rssi"] for s in samples if s["rssi"] is not None]
    counts = {name: 0 for name, _ in LEVELS[:len(thresholds) + 1]}
    counts[NO_SIGNAL[0]] = 0
    for s in samples:
        counts[level(s["rssi"], thresholds)[0]] += 1
    return {"n": len(samples), "min": min(vals) if vals else None, "max": max(vals) if vals else None,
            "median": statistics.median(vals) if vals else None, "levels": counts}


def to_geojson(samples, cells, thresholds, field=None) -> dict:
    feats = []
    for s in samples:
        name, color = level(s["rssi"], thresholds)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                      "properties": {"rssi_dbm": s["rssi"], "level": name, "color": color, "time": s["t"],
                                     "role": "base"}})
    for s in (field or []):
        name, color = level(s["rssi"], thresholds)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                      "properties": {"rssi_dbm": s["rssi"], "level": name, "color": color, "time": s["t"],
                                     "role": "field", "radio_id": s.get("radio_id", "")}})
    for c in cells:
        name, color = level(c["rssi"], thresholds)
        ring = [[c["lon0"], c["lat0"]], [c["lon1"], c["lat0"]], [c["lon1"], c["lat1"]],
                [c["lon0"], c["lat1"]], [c["lon0"], c["lat0"]]]
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"rssi_dbm": c["rssi"], "level": name, "color": color, "samples": c["n"],
                                     "kind": "grid"}})
    return {"type": "FeatureCollection", "features": feats}


def _kml_color(hex_rgb: str, alpha: str = "ff") -> str:
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"{alpha}{b}{g}{r}"


def to_kml(samples, cells, thresholds, title: str, field=None) -> str:
    styles = []
    for name, color in LEVELS + [NO_SIGNAL]:
        sid = name.replace(" ", "_")
        styles.append(
            f'<Style id="p_{sid}"><IconStyle><color>{_kml_color(color)}</color><scale>0.5</scale>'
            '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/shaded_dot.png</href></Icon></IconStyle>'
            '<LabelStyle><scale>0</scale></LabelStyle></Style>'
            f'<Style id="g_{sid}"><LineStyle><width>0</width></LineStyle>'
            f'<PolyStyle><color>{_kml_color(color, "99")}</color></PolyStyle></Style>')
    pts = []
    for s in samples:
        name, _ = level(s["rssi"], thresholds)
        val = "nessun segnale" if s["rssi"] is None else f"{s['rssi']:.1f} dBm"
        pts.append(f'<Placemark><name>{escape(val)}</name><styleUrl>#p_{name.replace(" ", "_")}</styleUrl>'
                   f'<Point><coordinates>{s["lon"]:.7f},{s["lat"]:.7f},0</coordinates></Point></Placemark>')
    polys = []
    for c in cells:
        name, _ = level(c["rssi"], thresholds)
        val = "nessun segnale" if c["rssi"] is None else f"{c['rssi']:.1f} dBm"
        coords = " ".join(f"{lo:.7f},{la:.7f},0" for lo, la in (
            (c["lon0"], c["lat0"]), (c["lon1"], c["lat0"]), (c["lon1"], c["lat1"]),
            (c["lon0"], c["lat1"]), (c["lon0"], c["lat0"])))
        polys.append(f'<Placemark><name>{escape(val)} ({c["n"]})</name>'
                     f'<styleUrl>#g_{name.replace(" ", "_")}</styleUrl><Polygon><outerBoundaryIs><LinearRing>'
                     f'<coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>')
    fpts = []
    for s in (field or []):
        name, _ = level(s["rssi"], thresholds)
        val = "nessun segnale" if s["rssi"] is None else f"{s['rssi']:.1f} dBm"
        rid = s.get("radio_id", "")
        fpts.append(f'<Placemark><name>Radio {escape(rid)}: {escape(val)}</name>'
                    f'<styleUrl>#p_{name.replace(" ", "_")}</styleUrl>'
                    f'<Point><coordinates>{s["lon"]:.7f},{s["lat"]:.7f},0</coordinates></Point></Placemark>')
    field_folder = f'<Folder><name>Radio sul campo</name>{"".join(fpts)}</Folder>' if fpts else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f'<name>{escape(title)}</name>{"".join(styles)}'
            f'<Folder><name>Griglia</name>{"".join(polys)}</Folder>'
            f'<Folder><name>Punti base</name>{"".join(pts)}</Folder>{field_folder}</Document></kml>\n')


def to_html(samples, cells, thresholds, title: str, field=None) -> str:
    tpl = resources.files("sitesurvey").joinpath("templates/map.html").read_text(encoding="utf-8")
    legend = [{"name": n, "color": c, "from": t} for (n, c), t in zip(LEVELS, list(thresholds) + [None])]
    legend.append({"name": NO_SIGNAL[0], "color": NO_SIGNAL[1], "from": None})
    field = field or []
    radios = sorted({s.get("radio_id", "") for s in field if s.get("radio_id")})
    data = {"title": title, "samples": samples, "field": field, "cells": cells,
            "thresholds": list(thresholds), "legend": legend, "radios": radios,
            "stats": stats(samples, thresholds)}
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return tpl.replace("__TITLE__", escape(title)).replace("__DATA__", payload)


def export_session(src: str | Path, outdir: str | Path | None = None, cell_m: float = 50.0,
                   thresholds=DEFAULT_THRESHOLDS) -> dict[str, Path]:
    src = Path(src)
    sess_dir = src if src.is_dir() else src.parent
    out = Path(outdir) if outdir else sess_dir
    out.mkdir(parents=True, exist_ok=True)
    base, field = split_roles(load_samples(src))
    cells = grid(base, cell_m)   # la copertura si calcola sulla base (downlink)
    title = f"Copertura radio - {sess_dir.name}"
    files = {
        "html": out / "map.html",
        "kml": out / "coverage.kml",
        "geojson": out / "coverage.geojson",
    }
    files["html"].write_text(to_html(base, cells, thresholds, title, field), encoding="utf-8")
    files["kml"].write_text(to_kml(base, cells, thresholds, title, field), encoding="utf-8")
    files["geojson"].write_text(json.dumps(to_geojson(base, cells, thresholds, field)), encoding="utf-8")
    return files


def merge_sessions(sources: list[str | Path], dest: str | Path) -> Path:
    """Unisce piu' sessioni in un unico samples.csv (mappa complessiva di piu' giri)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    header = None
    with (dest / "samples.csv").open("w", newline="") as fo:
        w = None
        for s in sources:
            p = Path(s)
            p = p / "samples.csv" if p.is_dir() else p
            with p.open(newline="") as fi:
                r = csv.reader(fi)
                h = next(r, None)
                if w is None:
                    header, w = h, csv.writer(fo)
                    w.writerow(header)
                for row in r:
                    w.writerow(row)
    return dest
