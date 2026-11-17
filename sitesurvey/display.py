"""Display OLED I2C (SSD1306/SH1106, es. Adafruit PiOLED 128x32) + pulsante start/stop su GPIO.

Mostra solo: stato radio, stato GPS, stato registrazione.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


def status_lines(snap: dict, blink: bool) -> list[str]:
    r = snap["radio"]
    if snap["radio_connected"]:
        name = (r.get("model") or r.get("vendor", "")).upper()[:9]
        radio = f"RADIO OK {name} {r.get('mode', '').upper()}"
    else:
        radio = "RADIO -- non conn."
    g = snap["gps"]
    if g["valid"]:
        sats = f"{g['sats']}sat " if g.get("sats") else ""
        gps = f"GPS FIX {sats}{(g.get('source') or '')[:5]}"
    else:
        gps = "GPS -- no fix"
    if snap["recording"]:
        e = snap["elapsed_s"]
        dot = "*" if blink else " "
        rec = f"REC{dot}{e // 3600:02d}:{e % 3600 // 60:02d}:{e % 60:02d} n{snap['samples']}"
    else:
        rec = "STOP  [premi=REC]"
    lines = [radio, gps, rec]
    if snap.get("message") and not snap["recording"]:
        lines.append(snap["message"][:21])
    elif snap["recording"] and snap.get("last_rssi") is not None:
        lines.append(f"RSSI {snap['last_rssi']:.0f} dBm")
    return lines


class Display(threading.Thread):
    def __init__(self, cfg: dict, surveyor):
        super().__init__(name="display", daemon=True)
        self.cfg = cfg
        self.sv = surveyor
        self._stop = threading.Event()
        self.device = None
        self.button = None
        self.led = None

    def _init_hw(self) -> None:
        drv = self.cfg["driver"]
        if drv != "none":
            try:
                from luma.core.interface.serial import i2c
                from luma.oled import device as dev
                serial = i2c(port=int(self.cfg["i2c_port"]), address=int(self.cfg["i2c_address"]))
                cls = {"ssd1306": dev.ssd1306, "sh1106": dev.sh1106}[drv]
                self.device = cls(serial, width=int(self.cfg["width"]), height=int(self.cfg["height"]),
                                  rotate=int(self.cfg["rotate"]))
            except Exception as e:  # noqa: BLE001
                log.error("display non inizializzato (%s): %s", drv, e)
        pin = int(self.cfg["button_gpio"])
        if pin >= 0:
            try:
                from gpiozero import Button
                self.button = Button(pin, pull_up=True, bounce_time=0.08)
                self.button.when_pressed = lambda: self.sv.toggle_recording()
            except Exception as e:  # noqa: BLE001
                log.error("pulsante GPIO%d non disponibile: %s", pin, e)
        lpin = int(self.cfg.get("led_gpio", -1))
        if lpin >= 0:
            try:
                from gpiozero import LED
                self.led = LED(lpin)
            except Exception as e:  # noqa: BLE001
                log.error("LED GPIO%d non disponibile: %s", lpin, e)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._init_hw()
        if not self.device and not self.led:
            return
        blink = False
        while not self._stop.is_set():
            snap = self.sv.state.snapshot()
            blink = not blink
            if self.led:
                self.led.value = 1 if snap["recording"] and blink else 0
            if self.device:
                try:
                    self._draw(status_lines(snap, blink))
                except Exception as e:  # noqa: BLE001
                    log.warning("errore display: %s", e)
                    time.sleep(2)
            self._stop.wait(0.5)
        if self.device:
            try:
                self.device.clear()
            except Exception:  # noqa: BLE001
                pass

    def _draw(self, lines: list[str]) -> None:
        from luma.core.render import canvas
        h = int(self.cfg["height"])
        n = 3 if h <= 32 else min(len(lines), h // 12)
        step = h // n
        with canvas(self.device) as draw:
            for i, text in enumerate(lines[:n]):
                draw.text((0, i * step - 1), text, fill="white")


def make_display(cfg: dict, surveyor):
    """Sceglie il backend display in base a cfg['driver'].

    ssd1306 | sh1106 -> OLED I2C (luma.oled)
    fb               -> TFT SPI a colori su framebuffer (es. 3.5" 480x320)
    none             -> nessun display (ma il pulsante GPIO resta attivo)
    """
    drv = cfg.get("driver", "none")
    if drv == "fb":
        from .fbdisplay import FbDisplay
        return FbDisplay(cfg, surveyor)
    return Display(cfg, surveyor)
