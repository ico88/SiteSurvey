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
| **RSSI numerico** | XCMP Radio Status, condizione RSSI `0x02` → `-(b0 + b1/256)` dBm | **Metodo del tool ufficiale** |

### GPS — nessuna licenza
Il GPS funziona senza XCMP e senza chiavi: basta abilitare GPS e "Location"/
"Data" nel CPS della radio. La radio invia i report su UDP 4001 (ascolto
passivo) e risponde alla richiesta immediata. In più l'ARS su 4005 dice quali
radio sono accese e mappa IP→Radio ID.

#### Base + radio sul campo (topologia "entrambe insieme")
Il GPS è tracciato **per Radio ID**:
- i report della **radio base** collegata arrivano dall'host locale
  (`192.168.10.1`) → etichetta `base`;
- i report via etere delle **radio sul campo** arrivano dal loro IP CAI
  (`12.x.y.z`): il Radio ID si ricava dall'IP, così ogni radio resta separata.

Con `survey.track_field_radios = true` ogni ciclo registra un campione `base`
(GPS base + RSSI downlink della base) **e** un campione `field` per ogni radio
sul campo con fix fresco. Nella mappa la copertura (griglia + statistiche) è
calcolata **solo sulla base** (downlink pulito); le radio sul campo compaiono
come marcatori a rombo separati per Radio ID. L'RSSI uplink per-radio è un
reverse a parte (quale evento `b4xx` porta il livello del chiamante): finché non
è risolto, i punti `field` hanno RSSI vuoto.

#### Formato della richiesta LRRP da Wireshark
Se catturi con Wireshark il pacchetto di richiesta di un programma che già
funziona, incollalo in `lrrp_request_template` (hex). Il segnaposto `{seq}` viene
sostituito col contatore a 1 byte in ASCII-hex. Così il formato esatto si adatta
alla tua radio/firmware senza toccare il codice. Con `lrrp_passive_only = true`
non si invia nulla e si usano solo i report periodici configurati nel CPS.

### Auth XCMP (solo per beacon PTT e RSSI)
La radio manda una challenge `[magic 2B][random 8B]` e si risponde con
`TEA(random8, chiave16)` (TEA classico, 32 round, big-endian). Due modi:

1. **Chiave XNL** (consigliato): con la chiave corretta (16 byte) l'auth riesce
   ad **ogni** tentativo. Si mette in `[radio.motorola] xnl_key = "..."`. La
   chiave è un **segreto Motorola** e **non è nel repository**: va copiata dal
   proprio `dmr-tracker` (`radioserver.py`, `XNL_KEY`) nel config locale del Pi.
2. **Lookup** (fallback senza chiave): tabella `random → risposta` da catture
   Wireshark in `[radio.motorola.xcmp_lookup]` (tre coppie incluse). Il random si
   ripete con periodo ~170, quindi servono molti tentativi e molte coppie.

L'implementazione TEA è verificata: riproduce esattamente le coppie
challenge/response catturate sul campo.

### RSSI numerico — metodo ufficiale (XCMP Radio Status)
Ricavato per interoperabilità dal **MOTOTRBO Site Survey ufficiale** (decompilazione
di `Mototrbo.Xcmp.dll`, posseduto dall'utente — sono fatti di protocollo, nessun
segreto):

- opcode **`RadioStatusRequest = 0x000E`**, reply **`RadioStatusReply = 0x800E`**;
- `StatusCondition.ReadRssi = 0x02` (altre: ModelNumber 0x07, SerialNumber 0x08,
  RadioID 0x0E);
- la radio risponde con due byte di valore e il dBm è:

  **`RSSI = -(byte0 + byte1 / 256)`**

  (parte intera in dBm + frazione in 1/256). Es. `5F 40` → −(95 + 64/256) = −95.25 dBm.

Config: `[radio.motorola.rssi] method="radiostatus"`. Richiede XCMP autenticato
(`xcmp_enabled=true` + coppie `xcmp_lookup`). Nessuna taratura: il valore è già in
dBm.

> Il **trasporto** della richiesta (framing XNL/XCMP) riusa il percorso provato
> per il PTT: su alcuni modelli il formato esatto può differire. Avendo ora il
> tool ufficiale, puoi **catturarlo con Wireshark** mentre fa una lettura RSSI e
> confermare i byte. Resta disponibile il ripiego `method="disptxt_menu"` (menu
> RSSI nascosto + testo del display).

## Hytera

| Funzione | Come | Stato |
|---|---|---|
| Trasporto/sessione | HRNP su USB bulk, handshake CONNECT→ACCEPT | **Verificato sul campo** (MD785i) |
| Stato canale / busy | RCP `0x00E7` (freq RX/TX, carrier/occupato) | **Verificato sul campo** |
| **GPS + RSSI insieme** | Location Protocol: `StandardReportWithRSSI 0xA004` | Implementato (riferimento ok-dmrlib) |
| GPS | LP `StandardRequest 0xA001` → report (ASCII tipo-NMEA) | Implementato |
| Beacon | PTT breve (RCPButton FRONT_PTT) | Implementato |
| Livello segnale grezzo | tag `0x09` del `0x00E7` (fallback) | Esposto |

### GPS + RSSI via Location Protocol (consigliato)
Il Location Protocol Hytera (servizio LP, `0x08`) restituisce **posizione e RSSI
nello stesso report**: si attivano i report con RSSI (`RSSIReportConfigRequest
0xE001`) e si interroga la radio (`StandardRequest 0xA001`); la risposta
(`StandardReport 0xA002` o `StandardReportWithRSSI 0xA004`) contiene il blocco
GPS (ASCII tipo-NMEA) e, nelle varianti RSSI, il valore dopo il GPS.

In `[radio.hytera]` imposta `radio_id` (l'ID della radio base) e in
`[radio.hytera.rssi]` `method="lp"`. Il valore RSSI grezzo si mappa in dBm con
`scale`/`offset` (da confermare sul modello). La struttura è ripresa dalla
libreria **OK-DMR/ok-dmrlib** (AGPL, qui **riscritta** per non vincolare la
licenza del progetto).

> Nota: senza `radio_id` le query LP non partono — in quel caso usa un GPS USB
> esterno (`gps.source = "nmea"`) e, per l'RSSI, il fallback
> `method="channel_status_level"` (livello grezzo tag `0x09`, da tarare).

## GPS esterno (consigliato)
Un ricevitore GPS USB (`/dev/ttyACM0`) con `gps.source = "auto"` diventa la
sorgente principale quando ha un fix, con fallback al GPS della radio. Dà la
posizione anche in analogico o con radio senza GPS, e sincronizza l'orologio del
Pi (che offline non ha RTC).

## Crediti e licenze dei riferimenti
- Parser LRRP/ARS e transport Hytera USB: portati dai moduli verificati sul campo
  di `ico88/raspradio` e `ico88/DMR-Tracker`.
- RSSI Motorola (XCMP Radio Status, condizione 0x02, formula `-(b0+b1/256)`),
  opcode 0x000E/0x800E/0x0415: ricavati per **interoperabilità** dal MOTOTRBO
  Site Survey ufficiale posseduto dall'utente (decompilazione delle sue DLL
  .NET). Sono informazioni di protocollo necessarie a far dialogare le proprie
  radio; **nessuna chiave o segreto** è estratto o incluso. Il ripiego
  `disptxt_menu` è ispirato a **nr23730/trboxcmp** (MIT).
- Struttura del Location Protocol Hytera (GPS + RSSI): studiata su
  **OK-DMR/ok-dmrlib** — licenza **AGPL-3.0**. Qui è **riscritta da zero**: non è
  incluso né linkato codice AGPL, così SiteSurvey non eredita l'AGPL.

Scala coordinate LRRP (`lat·90/2³¹` con segno) e token posizione verificati per
confronto con `ok-dmrlib` e con i dati di campo del DM4601e.
