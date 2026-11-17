# Protocolli radio: cosa è implementato e cosa va tarato

Questo codice **riusa i moduli già verificati sul campo** nei repository
`ico88/raspradio` e `ico88/DMR-Tracker`. Le differenze rispetto alle prime
ipotesi teoriche sono importanti: qui c'è il riepilogo di cosa è provato e cosa
resta da tarare su hardware.

## Collegamento

| Vendor | Trasporto controllo | Trasporto dati |
|---|---|---|
| Motorola | — (GPS/ARS non richiedono controllo) · XCMP su **TCP 8002** (solo beacon PTT e RSSI) | **USB-RNDIS**, radio `192.168.10.1` |
| Hytera | **HRNP su USB bulk** (endpoint 0x04/0x84, VID `238B`), via `pyusb` | USB bulk |

Verifica Motorola (rete):
```bash
ip addr            # usb0 con 192.168.10.x
ping 192.168.10.1
```
Verifica Hytera (USB):
```bash
lsusb | grep -i 238b
```

## Motorola MOTOTRBO

| Funzione | Come | Stato |
|---|---|---|
| GPS | LRRP UDP 4001: ascolto passivo dei report `0x0D` + richiesta immediata. Token posizione `0x55`. | **Verificato sul campo** (DM4601e) |
| Radio in aria / IP→ID | ARS UDP 4005 | **Verificato sul campo** |
| Beacon DMR | pacchetto UDP verso l'IP CAI (`12.x.y.z`, gruppi `225.x.y.z`) | Implementato |
| Beacon analogico | PTT via XCMP (subop `0x0415`) | Richiede XCMP autenticato |
| Auth XCMP | challenge/response a **lookup** (nessuna chiave ADK) | Dai dump; servono le coppie |
| **RSSI numerico** | notifica XCMP `b40e` "RSSI_OR_SIGNAL" | **DA REVERSE-ENGINEERARE** |

### GPS — nessuna licenza
Il GPS funziona senza XCMP e senza chiavi: basta abilitare GPS e "Location"/
"Data" nel CPS della radio. La radio invia i report su UDP 4001 (ascolto
passivo) e risponde alla richiesta immediata. In più l'ARS su 4005 dice quali
radio sono accese e mappa IP→Radio ID.

### Auth XCMP (solo per beacon PTT e RSSI)
La radio manda una challenge `[magic 2B][random 8B]`; il random si ripete con
periodo ~170 connessioni. Per ogni random noto si conosce la risposta (da
catture Wireshark). Si ritenta la connessione finché la challenge cade su un
random in tabella. Le coppie vanno in `[radio.motorola.xcmp_lookup]`; tre sono
già incluse come default. **Non serve la chiave ADK/TEA.**

### RSSI numerico — da reverse-engineerare
Nei repo esistenti la notifica `b40e` ("RSSI_OR_SIGNAL") viene solo ACK-ata, mai
decodificata in un valore. Qui la sessione XCMP **raccoglie** quelle notifiche;
il byte che contiene l'RSSI va individuato su hardware:

```bash
sudo systemctl stop sitesurvey
# abilita xcmp_enabled=true e metti le coppie lookup, poi:
sitesurvey probe --driver motorola --rssi-scan --beacon -n 30
```
Fai una misura **vicino al ponte** e una **lontano**: confronta i frame, trova
il byte che cambia con il segnale e impostalo in `[radio.motorola.rssi]`
(`method="xcmp_notify"`, `value_offset`, `value_len`, `scale`, `offset`).
Finché non è tarato, `method="none"` e l'RSSI resta vuoto (onesto: meglio
nessun dato che un dato finto).

## Hytera

| Funzione | Come | Stato |
|---|---|---|
| Trasporto/sessione | HRNP su USB bulk, handshake CONNECT→ACCEPT | **Verificato sul campo** (MD785i) |
| Stato canale / busy | RCP `0x00E7` (freq RX/TX, carrier/occupato) | **Verificato sul campo** |
| Livello segnale grezzo | tag `0x09` del `0x00E7` | Esposto, **non in dBm** |
| Beacon | PTT breve (RCPButton FRONT_PTT) | Implementato |
| **RSSI numerico** | dal livello `0x09`, o opcode candidati `0x0227`/`0x0201` | **DA TARARE** |
| GPS via radio | servizio LP | **Non validato** → usare GPS USB |

### RSSI Hytera — da tarare
Il `0x00E7` restituisce un livello grezzo (tag `0x09`), significativo soprattutto
quando c'è portante (busy). Per trasformarlo in dBm:
```bash
sitesurvey probe --driver hytera --rssi-scan --beacon -n 30
```
Interroga anche gli opcode candidati `0x0227`/`0x0201`. Trovata la relazione
livello→dBm, imposta `[radio.hytera.rssi]` con `method="channel_status_level"`,
`scale`, `offset`.

### GPS Hytera
Il formato LP non è ancora validato, quindi `get_location()` è disattivato: con
Hytera usa un **GPS USB esterno** (`gps.source = "nmea"`).

## GPS esterno (consigliato)
Un ricevitore GPS USB (`/dev/ttyACM0`) con `gps.source = "auto"` diventa la
sorgente principale quando ha un fix, con fallback al GPS della radio. Dà la
posizione anche in analogico o con radio senza GPS, e sincronizza l'orologio del
Pi (che offline non ha RTC).

## Crediti
Parser LRRP/ARS e transport Hytera USB sono portati dai moduli verificati sul
campo di `ico88/raspradio` (`main.py`) e `ico88/DMR-Tracker` (`hytera_link.py`,
`xcmp.py`).
