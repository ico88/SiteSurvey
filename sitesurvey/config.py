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
            "base_radio_id": "",       # Radio ID della radio base (etichetta campioni)
            "lrrp_port": 4001,
            "lrrp_local_port": 4001,
            "lrrp_passive_only": False,  # True = solo report periodici, niente richiesta
            "lrrp_request_template": "",  # hex da Wireshark; {seq} = contatore ASCII-hex
            "poll_field_radios": False,   # interroga le radio online viste dall'ARS
            "ars_enabled": True,
            "ars_port": 4005,
            # XCMP (TCP 8002): serve SOLO per beacon PTT e per il reverse RSSI.
            # Auth challenge/response a lookup (nessuna chiave ADK).
            "xcmp_enabled": False,
            "xcmp_port": 8002,
            "xcmp_max_attempts": 300,
            # Auth XNL: con la chiave TEA (16 byte hex) autentica ad ogni tentativo.
            # NON inclusa qui (segreto): copiala nel config dal tuo dmr-tracker.
            "xnl_key": "",
            "xcmp_lookup": {},         # fallback: {"<random hex>": "<risposta hex>", ...}
            "cai_network": 12,
            "cai_group_network": 225,
            # RSSI numerico Motorola:
            #  radiostatus  = XCMP Radio Status, condizione RSSI 0x02 -> dBm
            #                 con -(b0 + b1/256). E' il metodo del tool ufficiale.
            #  disptxt_menu = ripiego: menu RSSI nascosto + testo del display
            #  xcmp_notify  = decodifica una notifica XCMP (offset da tarare)
            #  none         = non misurato
            "rssi": {
                "method": "radiostatus",
                "reply_wait_s": 0.25,
                "menu_sequence": ["left", "left", "left", "right", "right", "right"],
                "text_regex": "",
                "notify_subop": 0xB40E,
                "value_offset": 0,
                "value_len": 1,
                "signed": True,
                "scale": 1.0,
                "offset": 0.0,
            },
        },
        "hytera": {
            "usb": {"pid": "", "serial": ""},   # vuoto = primo device Hytera trovato
            "radio_id": 0,             # Radio ID della radio base (per le query LP)
            "cai_network": 10,
            # RSSI numerico Hytera:
            #  lp                    = dal Location Protocol (GPS+RSSI insieme, 0xA004)
            #  channel_status_level  = livello grezzo dal channel-status (tag 0x09)
            #  none                  = non misurato
            "rssi": {
                "method": "lp",        # lp | channel_status_level | none
                "scale": 1.0,
                "offset": 0.0,
            },
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
        "track_field_radios": False,  # registra anche le radio sul campo (per Radio ID)
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
        "driver": "fb",                # fb | ssd1306 | sh1106 | none
        # --- TFT SPI a colori su framebuffer (driver = "fb"), es. 3.5" 480x320
        "fb_device": "auto",           # auto = trova il TFT da solo (o /dev/fb0, /dev/fb1)
        "bpp": 16,                     # RGB565
        "bgr": False,                  # True se i colori appaiono invertiti R<->B
        "touch_enabled": True,         # tocca lo schermo per avviare/fermare
        "touch_device": "",            # "" = autodetect (XPT2046)
        "touch_debounce_s": 1.0,
        # --- OLED I2C (driver = "ssd1306"/"sh1106")
        "i2c_port": 1,
        "i2c_address": 0x3C,
        # width/height: OLED (128x32); per "fb" si leggono da /dev/fb1
        "width": 480,
        "height": 320,
        "rotate": 0,                   # 0/90/180/270 (orientamento pannello)
        "button_gpio": 5,              # pulsante GPIO opzionale; -1 per disabilitare
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
