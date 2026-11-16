# Protocolli radio: cosa è implementato e cosa va verificato

Il software parla con la radio **solo via USB**. Sia MOTOTRBO sia Hytera, collegate
via USB, si presentano al Raspberry come **scheda di rete** (`cdc_ether` / `rndis_host`).
La radio ha IP `192.168.10.1` e assegna al Pi un indirizzo via DHCP. Verifica:

```bash
ip addr          # deve comparire usb0 / eth1 con 192.168.10.x
ping 192.168.10.1
```

## Motorola MOTOTRBO

| Funzione | Protocollo | Porta | Stato |
|---|---|---|---|
| RSSI | XNL/XCMP, *Radio Status* (0x000E) cond. 0x02 | TCP 8002 | Implementato. **Richiede chiave XNL** |
| Modello/seriale/ID | XCMP *Radio Status* 0x07/0x08/0x0E | TCP 8002 | Implementato |
| GPS | LRRP *Immediate Location Request* | UDP 4001 | Implementato |
| Beacon DMR | Pacchetto UDP verso l'IP CAI del destinatario (`12.x.y.z`, gruppi `225.x.y.z`) | UDP | Implementato |
| Beacon analogico | XCMP PTT (opcode/dati configurabili) | TCP 8002 | Da verificare sul modello |

### Chiave XNL
Senza autenticazione XNL la radio non risponde a nessun comando XCMP, quindi
l'RSSI non si legge. La chiave (4 word a 32 bit) e il `delta` li fornisce Motorola
Solutions agli sviluppatori registrati (programma **MOTOTRBO ADK** / Application
Developer Program). Il software non li include: vanno messi in
`radio.motorola.xnl_key` e `xnl_delta`.

### Configurazione CPS della radio
- *General Settings → GPS*: abilitato.
- *Network*: abilitare *Forward to PC* (USB). Controlla la *CAI Network* (default 12) e
  la *CAI Group Network* (default 225).
- *Channel*: *Data Call Confirmed* a piacere. Il canale deve permettere i dati.
- Per il beacon DMR: `beacon.target_id` deve essere un ID esistente (per esempio una
  console o una seconda radio) oppure un gruppo con `target_is_group = true`.

### Come funziona la misura
1. Il Pi invia un breve pacchetto dati verso `target_id`. La radio lo trasmette in
   DMR e il ripetitore si aggancia (*hang time*).
2. Dopo `settle_ms` il Pi legge l'RSSI `rssi_samples` volte e prende il valore indicato in
   `rssi_aggregate` (default: massimo, per scartare letture fatte prima dell'aggancio).
3. Registra il campione con la posizione GPS. Se non c'è segnale, il campione resta
   comunque registrato (cioè "nessun segnale") ed è un buco di copertura sulla mappa.

## Hytera

| Funzione | Protocollo | Porta (default) | Stato |
|---|---|---|---|
| Trasporto | HRNP (connect/accept/data/ack) | UDP o TCP | Implementato |
| GPS | HDAP LP, *Standard Immediate Location* (0xA001) | 30003 | Implementato (parsing tollerante) |
| RSSI | HDAP RCP, **opcode configurabile** | 30005 | **Da configurare** |
| Beacon DMR | UDP verso IP `10.x.y.z` | UDP | Implementato |
| Beacon analogico | RCP PTT (opcode/dati configurabili) | 30005 | Da configurare |

Gli opcode RCP che restituiscono l'RSSI cambiano a seconda di serie e firmware
(PD/MD/HP/HM) e sono documentati solo nell'**SDK Hytera** (*Hytera DMR Application
Development Kit*, accesso tramite il programma partner Hytera). Lo stesso vale per le
porte dei servizi. Quando hai il documento:

```toml
[radio.hytera]
rssi_opcode = 0x....        # opcode richiesta (la risposta attesa e' opcode | 0x8000)
rssi_request_data = "..."   # eventuali byte di richiesta (hex)
rssi_reply_offset = 0       # posizione del valore RSSI nella risposta
rssi_reply_len = 2
```

Verifica con:

```bash
sudo systemctl stop sitesurvey
sitesurvey -c /etc/sitesurvey/config.toml probe --driver hytera -v
```

## GPS esterno (consigliato come backup)
Un ricevitore GPS USB (u-blox o simili, `/dev/ttyACM0`) con `gps.source = "auto"`
diventa la sorgente principale quando ha un fix. Se non ha fix si usa il GPS della
radio. Così hai la posizione anche con radio senza GPS o in analogico, e l'orologio del Pi
viene sincronizzato dal GPS (`set_system_time`), visto che il Pi offline non ha un RTC.
