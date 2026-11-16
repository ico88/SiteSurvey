from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_config


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_run(args) -> int:
    from .control import ControlServer
    from .display import Display
    from .surveyor import Surveyor

    cfg = load_config(args.config)
    _setup_logging(cfg["general"]["log_level"])
    sv = Surveyor(cfg)
    ctl = ControlServer(cfg["general"]["control_socket"], sv)
    ctl.start()
    disp = Display(cfg["display"], sv)
    disp.start()

    def _term(*_):
        sv.shutdown()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        sv.run()
    finally:
        disp.stop()
        ctl.stop()
    return 0


def cmd_ctl(args) -> int:
    from .control import send_command
    cfg = load_config(args.config)
    try:
        resp = send_command(cfg["general"]["control_socket"], args.action)
    except OSError as e:
        print(f"servizio non raggiungibile ({cfg['general']['control_socket']}): {e}", file=sys.stderr)
        return 2
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 1 if "error" in resp else 0


def cmd_export(args) -> int:
    from .export import export_session, merge_sessions
    cfg = load_config(args.config)
    thresholds = cfg["export"]["thresholds"]
    cell = args.grid if args.grid is not None else float(cfg["export"]["grid_m"])
    src = args.session
    if len(src) > 1:
        out = Path(args.output or "merged")
        src_path = merge_sessions(src, out)
    else:
        src_path = Path(src[0])
        if not src_path.exists():
            cand = Path(cfg["general"]["data_dir"]) / "sessions" / src[0]
            if cand.exists():
                src_path = cand
    files = export_session(src_path, args.output, cell, thresholds)
    for k, p in files.items():
        print(f"{k:8s} {p}")
    return 0


def cmd_sessions(args) -> int:
    cfg = load_config(args.config)
    base = Path(cfg["general"]["data_dir"]) / "sessions"
    if not base.exists():
        print("nessuna sessione")
        return 0
    for d in sorted(base.iterdir()):
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            pass
        n = meta.get("samples")
        if n is None and (d / "samples.csv").exists():
            n = max(0, sum(1 for _ in (d / "samples.csv").open()) - 1)
        print(f"{d.name:22s} campioni={n}  driver={meta.get('driver', '?')}  modo={meta.get('mode', '?')}")
    return 0


def cmd_probe(args) -> int:
    """Diagnostica: connette la radio e stampa RSSI/GPS per N cicli."""
    from .radio import RadioError, make_driver
    cfg = load_config(args.config)
    _setup_logging("DEBUG" if args.verbose else "INFO")
    if args.driver:
        cfg["radio"]["driver"] = args.driver
    r = make_driver(cfg)
    try:
        r.connect()
    except RadioError as e:
        print(f"connessione fallita: {e}", file=sys.stderr)
        return 2
    print("connesso:", r.info())
    try:
        for i in range(args.count):
            if args.beacon:
                print("  beacon:", r.send_beacon(i + 1, cfg["beacon"]))
                time.sleep(float(cfg["beacon"]["settle_ms"]) / 1000)
            try:
                rssi = r.read_rssi()
            except RadioError as e:
                rssi = f"errore: {e}"
            try:
                fix = r.get_location()
            except RadioError as e:
                fix = f"errore: {e}"
            print(f"[{i + 1}] RSSI={rssi}  GPS={fix}")
            time.sleep(1)
    finally:
        r.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sitesurvey", description="Site survey radio DMR/analogico")
    ap.add_argument("-c", "--config", default=None, help=f"file di configurazione (default {DEFAULT_CONFIG_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="avvia il servizio (usato da systemd)").set_defaults(fn=cmd_run)

    p = sub.add_parser("ctl", help="controlla il servizio in esecuzione")
    p.add_argument("action", choices=["start", "stop", "toggle", "status"])
    p.set_defaults(fn=cmd_ctl)

    p = sub.add_parser("export", help="genera mappa HTML/KML/GeoJSON da una o piu' sessioni")
    p.add_argument("session", nargs="+", help="cartella sessione (o nome) o samples.csv; piu' di una = unione")
    p.add_argument("-o", "--output", help="cartella di destinazione (default: cartella sessione)")
    p.add_argument("--grid", type=float, default=None, help="lato cella griglia in metri (0 = niente griglia)")
    p.set_defaults(fn=cmd_export)

    sub.add_parser("sessions", help="elenca le sessioni registrate").set_defaults(fn=cmd_sessions)

    p = sub.add_parser("probe", help="test di comunicazione con la radio")
    p.add_argument("-n", "--count", type=int, default=5)
    p.add_argument("--beacon", action="store_true", help="invia un beacon prima di ogni lettura")
    p.add_argument("--driver", choices=["motorola", "hytera", "simulator"])
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_probe)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
