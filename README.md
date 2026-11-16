# SiteSurvey

Strumento di **site survey radio** per Raspberry Pi, sullo stile del *Site Survey tool*
Motorola. Legge da una radio collegata via USB la **posizione GPS** e l'**intensità
del segnale (RSSI)**, inviando dei **beacon** con la radio stessa (dati DMR o PTT in
analogico). Il risultato è una **mappa di copertura reale**.

- Radio: **Motorola MOTOTRBO** e **Hytera** (DMR, analogico), più un **simulatore**
- Funziona **completamente offline**. Comunicazione con la radio solo **via USB**
- **Servizio systemd** con avvio automatico e riavvio in caso di errore
- **Display OLED** I2C (PiOLED 128x32 / SSD1306 / SH1106) con le sole informazioni utili:
  stato radio, stato GPS, registrazione
- **Pulsante** su GPIO per avviare e fermare la registrazione
- Export: **mappa HTML offline** (punti e griglia colorati per livello), **KML** per
  Google Earth, **GeoJSON** per QGIS, **CSV** grezzo

```
┌──────────────┐  USB (rete 192.168.10.x)  ┌────────────────────────────┐
│ Radio DMR    │◄─────────────────────────►│ Raspberry Pi 2             │
│ Moto/Hytera  │  XCMP/RCP: RSSI, PTT      │  sitesurvey.service        │
│  + GPS int.  │  LRRP/LP:  GPS            │   ├ surveyor (ciclo misura)│
└──────────────┘  UDP:      beacon dati    │   ├ recorder (CSV)         │
                                           │   ├ display OLED + pulsante│
   GPS USB (opz.) ── NMEA ────────────────►│   └ socket di controllo    │
                                           └────────────────────────────┘
```

## Hardware

| Componente | Note |
|---|---|
| Raspberry Pi 2 (o successivo) | Raspberry Pi OS Lite (Bookworm o Bullseye) |
| Display | Adafruit **PiOLED 128x32** (SSD1306, I2C 0x3C) o altri SSD1306/SH1106 |
| Pulsante | Tra **GPIO5** (pin 29) e **GND** (pin 30). Il pin si cambia in `display.button_gpio` |
| Radio | MOTOTRBO (con chiave XNL) o Hytera, cavo USB di programmazione/dati |
| GPS USB (opz.) | Consigliato come backup e per sincronizzare l'orologio del Pi |

## Installazione sul Raspberry

```bash
git clone <questo repository> sitesurvey && cd sitesurvey
sudo ./scripts/install.sh
sudo nano /etc/sitesurvey/config.toml     # driver radio, chiave XNL, beacon...
sudo systemctl restart sitesurvey
journalctl -u sitesurvey -f
```

Per la prima installazione serve internet (pacchetti pip). Dopo, il sistema lavora offline.

## Uso sul campo

1. Accendi il Pi con la radio collegata. Il display mostra:
   ```
   RADIO OK DM4600 DMR
   GPS FIX 9sat radio
   STOP  [premi=REC]
   ```
2. Premi il pulsante per iniziare: `REC*00:12:34 n356`. Ripremi per fermare.
3. Ogni sessione viene salvata in `/var/lib/sitesurvey/sessions/AAAAMMGG-hhmmss/`.

Da terminale (SSH):

```bash
sitesurvey ctl status | start | stop | toggle
sitesurvey sessions
sitesurvey export 20261116-203501             # crea map.html, coverage.kml, coverage.geojson
sitesurvey export S1 S2 S3 -o giro-completo   # unisce piu' sessioni in un'unica mappa
sitesurvey probe -n 10 --beacon -v            # diagnostica radio (a servizio fermo)
```

Puoi fare l'export anche su un PC: copia la cartella della sessione e lancia
`pip install . && sitesurvey export <cartella>`. `map.html` si apre in qualsiasi browser
senza internet. Lo sfondo OpenStreetMap è facoltativo e si attiva da una casella.

## Livelli di copertura (default, modificabili in `[export]`)

| Livello | RSSI |
|---|---|
| Ottimo | ≥ −85 dBm |
| Buono | ≥ −95 dBm |
| Discreto | ≥ −105 dBm |
| Scarso | ≥ −115 dBm |
| Critico | < −115 dBm |
| Nessun segnale | nessuna lettura |

## Prova senza radio

```bash
pip install -e ".[test]"
python -m pytest
# servizio con radio simulata (percorso a spirale attorno a un sito)
sitesurvey -c config/simulator.toml run
```

## Stato dei protocolli (importante)

Vedi [docs/PROTOCOLLI.md](docs/PROTOCOLLI.md). In sintesi:

- **Motorola**: GPS (LRRP), beacon dati, RSSI e info radio (XNL/XCMP) sono implementati
  e testati contro una radio emulata. Per leggere l'RSSI serve la **chiave XNL del
  programma Motorola ADK**, che non può essere distribuita.
- **Hytera**: trasporto HRNP, GPS (LP) e beacon sono implementati. L'**opcode RCP per l'RSSI
  va configurato** dalla documentazione dell'SDK Hytera, perché dipende da modello e
  firmware.
- Il software non è ancora stato provato su radio reali: per la prima messa in servizio usa
  `sitesurvey probe -v`.

## Struttura

```
sitesurvey/
  radio/motorola.py  xnl.py  lrrp.py   driver MOTOTRBO
  radio/hytera.py                      driver Hytera (HRNP/HDAP)
  radio/simulator.py                   radio simulata
  surveyor.py                          ciclo di misura
  gps.py                               GPS NMEA esterno
  recorder.py                          salvataggio sessioni
  display.py                           OLED + pulsante
  control.py                           socket di controllo
  export.py  templates/map.html        mappe HTML/KML/GeoJSON
systemd/sitesurvey.service
scripts/install.sh
config/config.example.toml
```
