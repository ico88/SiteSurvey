# Installazione su Raspberry Pi — guida completa

Dal Raspberry "vuoto" a uno strumento di site survey funzionante, con display
TFT 3.5" a colori, radio via USB e GPS. Segui i passi in ordine.

> Legenda: i comandi `sudo ...` si danno sul Raspberry (via SSH o con
> tastiera/monitor). Il carattere `$` non va digitato.

---

## 0. Cosa serve

- Raspberry Pi 2 (o successivo) + alimentatore adeguato.
- MicroSD da almeno 8 GB.
- Display TFT **3.5" 480x320** (ILI9486 + touch XPT2046), quello che si infila
  sul connettore GPIO.
- Radio **Motorola MOTOTRBO** o **Hytera** con il suo **cavo USB** dati.
- (Consigliato) un **GPS USB** (es. u-blox) come backup e per l'orologio.
- Un PC per preparare la SD e per collegarti in SSH.

---

## 1. Preparare la scheda SD

1. Sul PC installa **Raspberry Pi Imager** (raspberrypi.com/software).
2. Scegli **Raspberry Pi OS Lite (32-bit)** (senza desktop: il display lo
   gestiamo noi).
3. Prima di scrivere, apri le **impostazioni** (icona ingranaggio / "Modifica
   impostazioni") e imposta:
   - **hostname**: es. `sitesurvey`;
   - **abilita SSH** (con password);
   - **utente/password** (es. utente `pi`);
   - **Wi-Fi** (SSID e password) se non usi il cavo di rete.
4. Scrivi la SD, inseriscila nel Raspberry, alimenta.

## 2. Primo accesso via SSH

Dal PC (stessa rete):

```bash
ssh pi@sitesurvey.local      # oppure ssh pi@<indirizzo-IP>
```

Aggiorna il sistema (serve internet; **solo per l'installazione**):

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

## 3. Abilitare SPI e l'overlay del display 3.5"

Il TFT è un pannello **SPI** che Linux vede come framebuffer `/dev/fb1`. Vai a
modificare il config del firmware:

```bash
sudo nano /boot/firmware/config.txt      # su sistemi vecchi: /boot/config.txt
```

Aggiungi in fondo:

```ini
dtparam=spi=on

# Pannello 3.5" ILI9486 + touch. Prova PRIMA questo:
dtoverlay=piscreen,speed=16000000,rotate=90
```

Salva (Ctrl-O, Invio) ed esci (Ctrl-X). Riavvia:

```bash
sudo reboot
```

Verifica che il framebuffer esista:

```bash
ls -l /dev/fb*                               # deve esserci /dev/fb1
cat /sys/class/graphics/fb1/virtual_size     # es. 480,320
```

- **Schermo bianco / niente `/dev/fb1`?** L'overlay giusto dipende dal clone.
  Prova, uno alla volta (riavviando ogni volta), `dtoverlay=waveshare35a`
  oppure `dtoverlay=mhs35,rotate=90`. Dettagli e touch in
  [docs/DISPLAY.md](DISPLAY.md).
- Se hai già usato lo script **LCD-show** (goodtft) su questo display, l'overlay
  è già a posto: salta questo passo.

## 4. Collegare la radio via USB

Collega la radio al Raspberry con il cavo dati.

**Motorola** (si presenta come scheda di rete USB):
```bash
ip addr                 # deve comparire un'interfaccia con 192.168.10.x
ping -c3 192.168.10.1   # la radio risponde
```
Nel CPS della radio abilita GPS e il servizio dati/Location (vedi
[docs/PROTOCOLLI.md](PROTOCOLLI.md)).

**Hytera** (controllo su USB bulk):
```bash
lsusb | grep -i 238b    # deve comparire il device Hytera (VID 238b)
```

## 5. Installare SiteSurvey

```bash
sudo apt install -y git
cd /home/pi
git clone -b claude/exciting-einstein-8nr4ev https://github.com/ico88/SiteSurvey.git
cd SiteSurvey
sudo ./scripts/install.sh
```

Lo script installa le dipendenze di sistema (libusb, SPI/I2C, font…), crea
l'utente di servizio `sitesurvey`, crea il virtualenv in `/opt/sitesurvey/venv`,
installa il pacchetto con gli extra `[pi]` (Pillow, numpy, evdev, pyusb,
luma.oled, gpiozero), copia la configurazione di esempio in
`/etc/sitesurvey/config.toml`, installa la regola udev per l'Hytera e **avvia il
servizio**.

> La prima installazione richiede internet (pip). Dopo, tutto funziona offline.

## 6. Configurare

Apri la configurazione:

```bash
sudo nano /etc/sitesurvey/config.toml
```

Imposta almeno:

```toml
[radio]
driver = "motorola"      # oppure "hytera"
mode   = "dmr"           # oppure "analog"

[display]
driver = "fb"            # TFT a colori; "rotate" solo se vuoi ruotare l'immagine
touch_enabled = true     # tocca lo schermo per avviare/fermare

[gps]
source = "auto"          # usa GPS USB se presente, altrimenti quello della radio
set_system_time = true
```

Se usi **Hytera** metti anche il suo `radio_id` (serve per GPS/RSSI via LP):

```toml
[radio.hytera]
radio_id = 2621001       # l'ID della radio base
```

Per il **beacon** (chi riceve il pacchetto che fa agganciare il ponte) imposta
`[beacon] target_id`. Dettagli e taratura RSSI in
[docs/PROTOCOLLI.md](PROTOCOLLI.md).

Applica le modifiche:

```bash
sudo systemctl restart sitesurvey
```

## 7. Verificare che funzioni

Stato del servizio e log in diretta:

```bash
sudo systemctl status sitesurvey
journalctl -u sitesurvey -f           # Ctrl-C per uscire
```

Sul **display** devi vedere la schermata a colori con RADIO / GPS / REC.
**Tocca lo schermo**: deve passare a REC e partire il cronometro; ritocca per
fermare.

Da terminale puoi controllare lo stato e comandare la registrazione:

```bash
sudo sitesurvey ctl status
sudo sitesurvey ctl start
sudo sitesurvey ctl stop
```

Diagnostica radio (a servizio fermo, per non occupare la porta):

```bash
sudo systemctl stop sitesurvey
sudo sitesurvey probe -v              # connessione, RSSI, GPS
sudo sitesurvey probe --rssi-scan -n 20   # per tarare l'RSSI (vedi PROTOCOLLI.md)
sudo systemctl start sitesurvey
```

## 8. Prima registrazione e mappa

1. Monta il Raspberry in auto con la radio e (se c'è) il GPS USB, parti con il
   giro e **tocca lo schermo** per registrare.
2. A fine giro, ferma la registrazione.
3. Elenca le sessioni ed esporta la mappa:

```bash
sitesurvey sessions
sitesurvey export <NOME-SESSIONE>     # crea map.html, coverage.kml, coverage.geojson
```

Le sessioni stanno in `/var/lib/sitesurvey/sessions/`. `map.html` si apre in
qualsiasi browser **senza internet**; `coverage.kml` si apre in Google Earth.
Puoi copiare la cartella su un PC via `scp` e fare lì l'export se preferisci.

## 9. Spegnere bene

Il sistema scrive su SD: evita di staccare l'alimentazione durante una
registrazione. Per spegnere:

```bash
sudo poweroff
```

(La registrazione è comunque scritta su disco in continuazione, quindi un calo
di tensione perde al massimo gli ultimi secondi.)

---

## Problemi frequenti

| Sintomo | Cosa controllare |
|---|---|
| Display nero, `/dev/fb1` assente | overlay sbagliato nel `config.txt` → prova `waveshare35a`/`mhs35` (passo 3, docs/DISPLAY.md) |
| Display acceso ma non disegna | `journalctl -u sitesurvey -f`; prova `cat /dev/urandom > /dev/fb1` (deve fare "neve") |
| Colori invertiti (rosso/blu) | `bgr = true` in `[display]` |
| Touch non reagisce | overlay touch (ads7846/xpt2046) caricato? `ls /dev/input/event*`; l'utente `sitesurvey` è nel gruppo `input` (lo fa l'installer) |
| "RADIO non connessa" (Motorola) | `ping 192.168.10.1`; GPS/dati abilitati nel CPS |
| "RADIO non connessa" (Hytera) | `lsusb \| grep 238b`; regola udev installata; radio accesa |
| Nessun fix GPS | all'aperto, attendi il primo fix; o collega un GPS USB (`source="auto"`) |
| RSSI vuoto | va tarato: `sitesurvey probe --rssi-scan` (vedi docs/PROTOCOLLI.md) |
| Permessi negati sui comandi | usa `sudo` (il socket di controllo è riservato al servizio) |
| `install.sh` fermo minuti su "Preparing metadata"/numpy | sta compilando numpy da sorgente. Non serve: `sudo apt install -y python3-numpy` e usa l'installer aggiornato (venv con `--system-site-packages`). Ctrl-C e rilancia. |

## Aggiornare in seguito

```bash
cd /home/pi/SiteSurvey
git pull
sudo /opt/sitesurvey/venv/bin/pip install ".[pi]"
sudo systemctl restart sitesurvey
```
