# Display TFT 3.5" 480x320 (ILI9486 + touch XPT2046)

Questo è il classico display "3.5inch RPi Display" che si infila sul connettore
GPIO. **Non** è un OLED I2C: è un **TFT SPI a colori** che Linux vede come
**framebuffer** (`/dev/fb1`). SiteSurvey ci disegna sopra la schermata di stato
(driver `display.driver = "fb"`) e, se abilitato, usa il **touch** per avviare e
fermare la registrazione.

## 1. Abilitare SPI e l'overlay del pannello

Il pannello va dichiarato come overlay nel firmware. Su Raspberry Pi OS
(`/boot/firmware/config.txt`, o `/boot/config.txt` sui sistemi più vecchi):

```ini
dtparam=spi=on

# Pannello 3.5" ILI9486 + touch ADS7846/XPT2046.
# Prova prima questo overlay generico:
dtoverlay=piscreen,speed=16000000,rotate=90
```

Se con `piscreen` lo schermo resta bianco, questi cloni spesso usano un overlay
dedicato. Alternative comuni, una alla volta:

```ini
# Waveshare 3.5 (A) e cloni blu come questo:
dtoverlay=waveshare35a
# oppure MHS-3.5 / goodtft (PCB rossi):
dtoverlay=mhs35,rotate=90
# oppure ILI9486 generico con flexfb (kernel con fbtft):
dtoverlay=flexfb,speed=16000000,rotate=90
dtoverlay=ads7846,cs=1,penirq=17,penirq_pull=2,speed=50000,keep_vref_on=1,xohms=150
```

> I cloni "3.5inch RPi Display" sono spesso venduti con lo script **LCD-show**
> (goodtft). Se lo hai già usato, l'overlay è probabilmente `tft35a`/`mhs35`:
> lascia quello e salta questo passo.

Dopo la modifica: `sudo reboot`. Verifica:

```bash
ls -l /dev/fb*            # deve comparire /dev/fb1
cat /sys/class/graphics/fb1/virtual_size   # es. 480,320  (o 320,480 se ruotato)
```

## 2. Orientamento

Il pannello di questo tipo è nativamente verticale (320×480). Con
`rotate=90`/`270` nell'overlay diventa orizzontale (480×320). Imposta
`display.rotate` nel config di SiteSurvey **solo** se vuoi ruotare ancora
l'immagine disegnata rispetto a come la mette l'overlay (0 va bene nella maggior
parte dei casi). Se i colori sembrano invertiti (rosso↔blu), metti `bgr = true`.

## 3. Touch (avvio/stop toccando lo schermo)

Con l'overlay `ads7846`/`xpt2046` caricato, il touch appare come dispositivo di
input. Verifica:

```bash
ls /dev/input/event*          # deve esserci un evento touch
# (facoltativo) libinput list-devices | grep -i -A3 touch
```

SiteSurvey trova il touch da solo (`touch_device = ""`). Un tocco fa partire o
ferma la registrazione (con antirimbalzo di 1 s). Non serve calibrazione perché
qualsiasi tocco è un "toggle". Se vuoi puntare un device preciso:
`touch_device = "/dev/input/eventN"`.

Il servizio gira come utente `sitesurvey`, che deve stare nel gruppo `input`
(lo fa l'installer). In alternativa si può sempre usare un pulsante fisico su
GPIO (`button_gpio`), che resta attivo in parallelo.

## 4. Config di SiteSurvey

```toml
[display]
driver = "fb"
fb_device = "/dev/fb1"
bpp = 16
bgr = false
rotate = 0
touch_enabled = true
touch_device = ""
```

## 5. Diagnostica

```bash
# schermo nero ma /dev/fb1 presente: prova a scriverci del rumore
cat /dev/urandom > /dev/fb1    # deve riempirsi di "neve"; Ctrl-C per fermare
# log del servizio
journalctl -u sitesurvey -f
```

Se `/dev/fb1` non esiste, l'overlay non è caricato correttamente: torna al
passo 1 e prova un overlay diverso. Finché manca `/dev/fb1`, SiteSurvey
continua a funzionare (registrazione, GPS, RSSI): semplicemente non disegna nulla
e lo segnala nel log.
