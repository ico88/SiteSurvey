"""Display TFT SPI a colori (es. 3.5" 480x320 ILI9486 + touch XPT2046).

Questi display si pilotano come framebuffer Linux (/dev/fb1), non come gli OLED
I2C. Qui si disegna la schermata di stato con Pillow e si scrive il buffer in
RGB565 sul framebuffer. Il touch (XPT2046) e' opzionale e serve per avviare e
fermare la registrazione toccando lo schermo, al posto del pulsante GPIO.

Setup del display (Raspberry Pi OS): abilitare SPI e caricare l'overlay del
pannello (vedi docs/DISPLAY.md). Quando c'e' /dev/fb1, questo driver funziona.
"""
from __future__ import annotations

import logging
import os
import struct
import threading
import time

log = logging.getLogger(__name__)

# Colori (R,G,B)
BG = (12, 16, 22)
CARD = (28, 34, 44)
WHITE = (236, 240, 246)
MUTED = (150, 160, 175)
GREEN = (46, 170, 90)
RED = (210, 70, 70)
AMBER = (230, 170, 60)
BLUE = (70, 130, 210)


def _fb_geometry(fbdev: str) -> tuple[int, int, int, int]:
    """(width, height, bpp, stride) letti da /sys, con fallback."""
    name = os.path.basename(fbdev)
    base = f"/sys/class/graphics/{name}"
    w = h = bpp = 0
    stride = 0
    try:
        with open(f"{base}/virtual_size") as f:
            w, h = (int(x) for x in f.read().strip().split(","))
        with open(f"{base}/bits_per_pixel") as f:
            bpp = int(f.read().strip())
        try:
            with open(f"{base}/stride") as f:
                stride = int(f.read().strip())
        except OSError:
            stride = 0
    except (OSError, ValueError):
        pass
    if not stride:
        stride = w * (bpp // 8) if w and bpp else 0
    return w, h, bpp, stride


def to_rgb565(img, bgr: bool = False) -> bytes:
    """Converte un'immagine Pillow RGB in RGB565 little-endian."""
    rgb = img.convert("RGB")
    try:
        import numpy as np
        a = np.asarray(rgb, dtype=np.uint16)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        if bgr:
            r, b = b, r
        out = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return out.astype("<u2").tobytes()
    except ImportError:
        pass
    data = rgb.tobytes()
    out = bytearray(len(data) // 3 * 2)
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        if bgr:
            r, b = b, r
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        struct.pack_into("<H", out, i // 3 * 2, v)
    return bytes(out)


class FbDisplay(threading.Thread):
    def __init__(self, cfg: dict, surveyor):
        super().__init__(name="fbdisplay", daemon=True)
        self.cfg = cfg
        self.sv = surveyor
        self._stop = threading.Event()
        self.fbdev = cfg.get("fb_device", "/dev/fb1")
        self.bgr = bool(cfg.get("bgr", False))
        self.rotate = int(cfg.get("rotate", 0))  # 0/90/180/270
        self.w = self.h = self.bpp = self.stride = 0
        self._font = None
        self._font_big = None
        self._font_huge = None
        self._touch: TouchInput | None = None

    # ---- font ------------------------------------------------------------
    def _load_fonts(self):
        from PIL import ImageFont
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        path = next((p for p in paths if os.path.exists(p)), None)
        if path:
            self._font = ImageFont.truetype(path, 22)
            self._font_big = ImageFont.truetype(path, 30)
            self._font_huge = ImageFont.truetype(path, 52)
        else:
            self._font = self._font_big = self._font_huge = ImageFont.load_default()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            log.error("Pillow non installato: display framebuffer disabilitato")
            return
        self.w, self.h, self.bpp, self.stride = _fb_geometry(self.fbdev)
        if not self.w:
            self.w = int(self.cfg.get("width", 480))
            self.h = int(self.cfg.get("height", 320))
            self.bpp = int(self.cfg.get("bpp", 16))
            self.stride = self.w * (self.bpp // 8)
        if self.bpp != 16:
            log.warning("framebuffer a %d bpp: supportato solo RGB565 (16 bpp)", self.bpp)
        if not os.path.exists(self.fbdev):
            log.error("framebuffer %s assente: display disabilitato (vedi docs/DISPLAY.md)", self.fbdev)
            return
        self._load_fonts()
        if self.cfg.get("touch_enabled", True):
            self._touch = TouchInput(self.cfg, self.sv)
            self._touch.start()
        log.info("display framebuffer %s %dx%d %dbpp", self.fbdev, self.w, self.h, self.bpp)
        blink = False
        while not self._stop.is_set():
            blink = not blink
            try:
                self._render(self.sv.state.snapshot(), blink)
            except Exception as e:  # noqa: BLE001
                log.warning("errore render display: %s", e)
                self._stop.wait(2)
            self._stop.wait(0.5)
        if self._touch:
            self._touch.stop()

    # ---- rendering -------------------------------------------------------
    def _render(self, snap: dict, blink: bool):
        from PIL import Image, ImageDraw
        # disegno sempre in orizzontale (W>=H) poi ruoto per il pannello
        land = self.rotate in (90, 270)
        cw, ch = (self.h, self.w) if land else (self.w, self.h)
        img = Image.new("RGB", (cw, ch), BG)
        d = ImageDraw.Draw(img)
        self._draw_content(d, cw, ch, snap, blink)
        if self.rotate:
            img = img.rotate(-self.rotate, expand=True)
        buf = to_rgb565(img, self.bgr)
        self._blit(buf)

    def _card(self, d, x, y, w, h, color):
        d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=CARD, outline=color, width=3)

    def _draw_content(self, d, W, H, snap, blink):
        pad = 10
        d.text((pad, 6), "SITE SURVEY", font=self._font_big, fill=WHITE)
        # riga radio/gps in alto
        card_h = 70
        half = (W - 3 * pad) // 2
        # RADIO
        r = snap["radio"]
        rc = GREEN if snap["radio_connected"] else RED
        self._card(d, pad, 48, half, card_h, rc)
        d.text((pad + 12, 54), "RADIO", font=self._font, fill=MUTED)
        if snap["radio_connected"]:
            name = (r.get("model") or r.get("vendor", "")).upper()
            d.text((pad + 12, 80), f"{name} {r.get('mode', '').upper()}", font=self._font, fill=WHITE)
        else:
            d.text((pad + 12, 80), "non connessa", font=self._font, fill=RED)
        # GPS
        gx = pad * 2 + half
        g = snap["gps"]
        gc = GREEN if g["valid"] else AMBER
        self._card(d, gx, 48, half, card_h, gc)
        d.text((gx + 12, 54), "GPS", font=self._font, fill=MUTED)
        if g["valid"]:
            sats = f"{g['sats']} sat" if g.get("sats") else "fix"
            d.text((gx + 12, 80), f"{sats} · {(g.get('source') or '')}", font=self._font, fill=WHITE)
        else:
            d.text((gx + 12, 80), "nessun fix", font=self._font, fill=AMBER)
        # blocco registrazione grande
        ry = 48 + card_h + pad
        rh = H - ry - 58
        rec = snap["recording"]
        self._card(d, pad, ry, W - 2 * pad, rh, RED if rec else BLUE)
        if rec:
            e = snap["elapsed_s"]
            dot = "●" if blink else "  "
            d.text((pad + 16, ry + 8), f"{dot} REC", font=self._font_big, fill=RED)
            d.text((pad + 16, ry + 48), f"{e // 3600:02d}:{e % 3600 // 60:02d}:{e % 60:02d}",
                   font=self._font_huge, fill=WHITE)
            # colonna destra: numero campioni e RSSI
            rx = W - 150
            d.text((rx, ry + 10), f"n. {snap['samples']}", font=self._font, fill=MUTED)
            rssi = snap.get("last_rssi")
            d.text((rx, ry + 44), f"{rssi:.0f} dBm" if rssi is not None else "-- dBm",
                   font=self._font_big, fill=WHITE if rssi is not None else MUTED)
        else:
            d.text((pad + 16, ry + 8), "STOP", font=self._font_big, fill=BLUE)
            d.text((pad + 16, ry + rh - 40), "tocca lo schermo per REGISTRARE",
                   font=self._font, fill=MUTED)
        # barra messaggi
        msg = snap.get("message") or ""
        if msg:
            d.text((pad, H - 44), msg[:48], font=self._font, fill=AMBER)

    def _blit(self, buf: bytes):
        row_bytes = self.w * 2
        with open(self.fbdev, "r+b") as f:
            if self.stride == row_bytes:
                f.write(buf)
            else:
                for y in range(self.h):
                    f.seek(y * self.stride)
                    f.write(buf[y * row_bytes:(y + 1) * row_bytes])


class TouchInput(threading.Thread):
    """Legge il touch XPT2046 (via evdev) e fa toggle della registrazione.

    Senza calibrazione: un tocco (rilascio) fa partire/fermare la registrazione,
    con un antirimbalzo. Richiede python-evdev e l'overlay touch caricato.
    """

    def __init__(self, cfg: dict, surveyor):
        super().__init__(name="touch", daemon=True)
        self.cfg = cfg
        self.sv = surveyor
        self.device = cfg.get("touch_device", "")  # "" = autodetect
        self.debounce = float(cfg.get("touch_debounce_s", 1.0))
        self._stop = threading.Event()
        self._last = 0.0

    def stop(self):
        self._stop.set()

    def _find_device(self):
        from evdev import InputDevice, ecodes, list_devices
        if self.device:
            return InputDevice(self.device)
        for path in list_devices():
            dev = InputDevice(path)
            caps = dev.capabilities()
            if ecodes.EV_ABS in caps and ecodes.EV_KEY in caps:
                keys = caps.get(ecodes.EV_KEY, [])
                if ecodes.BTN_TOUCH in keys:
                    return dev
        return None

    def run(self):
        try:
            from evdev import ecodes
        except ImportError:
            log.warning("python-evdev non installato: touch disabilitato (pulsante GPIO ancora attivo)")
            return
        while not self._stop.is_set():
            dev = None
            try:
                dev = self._find_device()
                if dev is None:
                    log.info("nessun dispositivo touch trovato; riprovo")
                    self._stop.wait(5)
                    continue
                log.info("touch: %s", dev.path)
                for event in dev.read_loop():
                    if self._stop.is_set():
                        break
                    if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH and event.value == 0:
                        now = time.time()
                        if now - self._last >= self.debounce:
                            self._last = now
                            self.sv.toggle_recording()
            except Exception as e:  # noqa: BLE001
                log.warning("touch: %s", e)
                self._stop.wait(3)
            finally:
                if dev:
                    try:
                        dev.close()
                    except Exception:  # noqa: BLE001
                        pass
