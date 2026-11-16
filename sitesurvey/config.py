from __future__ import annotations

import copy
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

DEFAULT_CONFIG_PATH = "/etc/sitesurvey/config.toml"

DEFAULTS: dict = {
    "general": {
        "data_dir": "/var/lib/sitesurvey",
        "log_level": "INFO",
        "control_socket": "/run/sitesurvey/control.sock",
    },
    "radio": {
        "driver": "motorola",          # motorola | hytera | simulator
        "mode": "dmr",                 # dmr | analog
        "reconnect_interval_s": 5.0,
        "motorola": {
            "host": "192.168.10.1",
            "xnl_port": 8002,
            "xnl_key": [0, 0, 0, 0],   # 4 word a 32 bit, dal Motorola ADK
            "xnl_delta": 0,
            "timeout_s": 3.0,
            "lrrp_port": 4001,
            "lrrp_local_port": 4001,
            "rssi_status_code": 0x02,
            "rssi_scale": 1.0,
            "rssi_offset": 0.0,
            "ptt_opcode": 0x0415,
            "ptt_key_data": "0101",
            "ptt_dekey_data": "0100",
            "cai_network": 12,
            "cai_group_network": 225,
        },
        "hytera": {
            "host": "192.168.10.1",
            "transport": "udp",        # udp | tcp
            "rcp_port": 30005,
            "lp_port": 30003,
            "timeout_s": 3.0,
            "radio_id": 1,             # ID della radio collegata (per richiesta LP)
            "cai_network": 10,
            "rssi_opcode": 0,          # 0 = non configurato. Vedi docs/PROTOCOLLI.md
            "rssi_request_data": "",
            "rssi_reply_offset": 0,
            "rssi_reply_len": 2,
            "rssi_scale": 1.0,
            "rssi_offset": 0.0,
            "ptt_opcode": 0x0041,
            "ptt_key_data": "",
            "ptt_dekey_data": "",
        },
        "simulator": {
            "site_lat": 45.4642,
            "site_lon": 9.1900,
            "tx_dbm": -25.0,
            "path_loss_exp": 3.2,
            "speed_kmh": 40.0,
            "radius_m": 3000.0,
        },
    },
    "beacon": {
        "enabled": True,
        "kind": "data",                # data | ptt | none
        "target_id": 1,
        "target_is_group": False,
        "target_port": 4007,
        "payload": "SITESURVEY",
        "settle_ms": 300,
        "ptt_ms": 500,
    },
    "survey": {
        "interval_s": 2.0,
        "idle_interval_s": 2.0,
        "min_distance_m": 0.0,
        "rssi_samples": 3,
        "rssi_sample_spacing_ms": 150,
        "rssi_aggregate": "max",      # max | mean | median
        "require_gps_fix": True,
        "auto_start": False,
        "fsync_every": 10,
    },
    "gps": {
        "source": "auto",              # radio | nmea | auto
        "nmea_device": "/dev/ttyACM0",
        "nmea_baud": 9600,
        "max_age_s": 5.0,
        "set_system_time": False,
    },
    "display": {
        "driver": "ssd1306",           # ssd1306 | sh1106 | none
        "i2c_port": 1,
        "i2c_address": 0x3C,
        "width": 128,
        "height": 32,
        "rotate": 0,
        "button_gpio": 5,              # -1 per disabilitare
        "led_gpio": -1,
    },
    "export": {
        # soglie RSSI in dBm (dal migliore al peggiore)
        "thresholds": [-85, -95, -105, -115],
        "grid_m": 50.0,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict:
    p = Path(path or DEFAULT_CONFIG_PATH)
    if not p.exists():
        if path:
            raise FileNotFoundError(f"File di configurazione non trovato: {p}")
        return copy.deepcopy(DEFAULTS)
    with p.open("rb") as f:
        return _merge(DEFAULTS, tomllib.load(f))
