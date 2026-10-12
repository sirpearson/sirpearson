# Monitor prezzi BnB Trieste

Ogni notte rileva su Booking prezzi e disponibilità della concorrenza nel centro di Trieste e mostra in una
dashboard dove si posiziona "Elegante appartamento in Marina" (Via Economo) rispetto al mercato, per decidere
i prezzi delle prossime 90 notti. Implementa la *Specifica di progetto* del 12/10/2026.

```
docker compose
├── db         PostgreSQL 16 (dati in ./data/postgres)
├── scraper    pianificatore: raccolta notturna, controllo qualità, metriche, Telegram, backup
└── dashboard  Streamlit su http://<pc-o-nas>:8501
```

## Avvio rapido (PC di casa)

1. Installare Docker (Docker Desktop su Windows/Mac, Docker Engine su Linux).
2. Copiare la configurazione dei segreti e compilarla:
   ```bash
   cp .env.example .env      # impostare POSTGRES_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
   ```
3. Sostituire `config/centro.geojson` con il poligono disegnato su [geojson.io](https://geojson.io)
   (quello presente è **provvisorio**, tracciato a mano in modo approssimativo).
4. Avviare:
   ```bash
   docker compose up -d --build
   docker compose exec scraper python -m bnbmonitor check-config    # verifica parametri
   docker compose exec scraper python -m bnbmonitor test-telegram   # messaggio di prova
   docker compose exec scraper python -m bnbmonitor run             # prima raccolta subito (facoltativo)
   ```
5. Aprire la dashboard su `http://localhost:8501`.

Il PC deve restare acceso di notte (finestra di raccolta predefinita: partenza casuale tra 01:30 e 03:30,
riepilogo Telegram alle 07:30).

### Provare la dashboard prima dei dati reali

```bash
docker compose exec scraper python -m bnbmonitor seed-demo --days 21
```

Inserisce dati **sintetici** (solo su database senza esecuzioni reali). Alla prima raccolta reale i dati demo
vengono cancellati automaticamente.

## Comandi

`docker compose exec scraper python -m bnbmonitor <comando>`

| Comando | Cosa fa |
|---|---|
| `run` | esegue subito il ciclo notturno completo |
| `check-config` | mostra profili, punto di ricerca, raggio di arresto, ricerche previste |
| `test-telegram` | invia un messaggio di prova |
| `print-summary` / `summary` | mostra / invia il riepilogo mattutino |
| `reclassify` | ricalcola poligono, distanze e segmenti dopo un cambio di configurazione |
| `refresh-metrics` | ricalcola tutte le metriche precalcolate |
| `backup` | esporta il database in `data/backups` |
| `seed-demo` | dati sintetici per provare la dashboard |

## Configurazione

Tutto in `config/config.yaml` (segreti esclusi, che stanno in `.env`):

- **Profili di ricerca** — aggiungere un profilo (es. 3 notti, 4 ospiti) è un blocco di poche righe.
  Fino a `near_days` si controlla ogni notte; oltre, ogni data una notte ogni `far_every_days`.
  Le date lontane ruotano allo stesso modo per tutti i profili, così i confronti 1 notte / 2 notti
  avvengono sulla stessa rilevazione.
- **BnB** — link di condivisione Booking; l'identificativo viene ricavato alla prima esecuzione
  (oppure si scrive in `own_property.booking_id`).
- **Zona** — file GeoJSON del poligono, punto di partenza della ricerca (default: baricentro del poligono),
  raggio di arresto (default: vertice più lontano + margine).
- **Prudenza** — pause casuali tra pagine, ricerche e schede; durata massima dell'esecuzione.
- **Controllo qualità** — soglie per esecuzione sospetta; giorni dopo cui una struttura diventa inattiva.
- **Classificazione** — codici tipologia Booking e parole chiave → segmento; correzioni manuali per singola
  struttura in `classification.overrides` (la pagina *Stato del sistema* elenca quelle da classificare).

Dopo una modifica: `docker compose restart scraper dashboard` e, se cambiano poligono o classificazione,
`python -m bnbmonitor reclassify`.

## Come funziona

**Raccolta.** Per ogni profilo e data Chromium (senza finestra, un solo browser, nessun login) cerca a partire
da un punto del centro con risultati ordinati per distanza e si ferma quando le strutture escono dal raggio di
arresto. Da ogni scheda dei risultati legge identificativo, nome, prezzo totale (con tasse e costi, secondo
`price_include_taxes`), valutazione, recensioni, colazione inclusa, cancellazione gratuita e posizione.
Le strutture nuove vengono aperte una volta per leggere coordinate e tipologia (al massimo
`max_new_details_per_run` a notte). Davanti a una verifica anti-bot si ferma subito e manda un allarme.

**Catena del dato.**

| Livello | Tabelle |
|---|---|
| Area di appoggio | `runs`, `staging_searches`, `staging_results` |
| Anagrafica | `properties` (segmento, coordinate, dentro il poligono, distanza dal BnB, attiva, è il BnB) |
| Osservazioni | `observations` — solo da esecuzioni valide, comprese le indisponibilità |
| Metriche | funzione `market_metrics(raggio)`, tabella `metrics_market` (centro), `min_stay_share(...)` |

**Controllo qualità.** Un'esecuzione è *valida*, *sospetta* (risultati per ricerca crollati rispetto alle
ultime esecuzioni valide, prezzi non leggibili, troppe ricerche incomplete), *fallita* o *bloccata*. Solo le
valide entrano nello storico; le altre restano nell'area di appoggio per l'analisi.

**Indisponibilità.** Una struttura attiva del centro assente da una ricerca *completa* è non disponibile per
quelle date. Dopo oltre 30 giorni senza comparire diventa inattiva ed esce dal calcolo.

**Metriche.** Per data di rilevazione, profilo, data di soggiorno e segmento (appartamenti, camere con o senza
hotel), sulla zona centro o sul raggio scelto in dashboard: mediana, 25°/75° percentile, tasso di disponibilità,
pickup e variazione mediana a 7 giorni, differenza weekend/infrasettimanale, curva per giorni all'arrivo,
differenza 1/2 notti, quota con soggiorno minimo 2 notti, indice di prezzo, percentile del BnB e prezzo contro
valutazione. Il raggio ravvicinato si calcola al momento.

**Dashboard.** Calendario, Dettaglio data, Mappa, Tendenze, Stato del sistema, con filtri sempre visibili per
profilo, segmento (con o senza hotel) e raggio da Via Economo.

**Telegram.** Allarmi immediati (blocco anti-bot, crollo dei risultati, prezzi non leggibili, esecuzione
fallita o saltata, backup fallito) e riepilogo ogni mattina: date con disponibilità in calo veloce,
movimenti rilevanti della mediana, date con il prezzo del BnB fuori dalla fascia 25°–75°.

## Trasferimento sul NAS

Requisiti: Docker con Compose (Synology, QNAP, UGREEN, TrueNAS, Unraid, OpenMediaVault), x86-64
(ARM64 possibile: l'immagine base Playwright esiste anche per ARM64), almeno 2 GB di RAM liberi di notte.

1. Sul PC: `docker compose down`.
2. Copiare l'intera cartella del progetto (codice, `config/`, `.env`, `data/`) sul NAS.
3. Sul NAS, nella cartella: `docker compose up -d --build`.
4. Includere `data/backups` nel sistema di backup del NAS. Per l'accesso da fuori casa usare l'accesso remoto
   del NAS verso la porta della dashboard.

Ripristino da un backup: `gunzip -c data/backups/bnb-AAAAMMGG-hhmmss.sql.gz | docker compose exec -T db psql -U bnb -d bnb`
(su un database vuoto).

## Sviluppo e test

```bash
pip install -r requirements.txt
pytest                                                     # test unitari
TEST_DATABASE_URL=postgresql://... pytest                  # + test end-to-end (il database viene svuotato)
```

Nel container: `docker compose exec -e TEST_DATABASE_URL=... scraper python -m pytest tests`.

### Da verificare con Booking reale

Lo sviluppo è avvenuto senza accesso a Booking: parametri di ricerca e selettori sono quelli noti, da
confermare alle prime esecuzioni. Le pagine HTML delle ultime esecuzioni sono salvate in `data/debug/`.

- parametri degli indirizzi di ricerca (`booking.extra_params`: `dest_type=latlong`, `order=distance_from_search`)
  e paginazione con `offset`;
- selettori delle schede (`bnbmonitor/booking_parse.py`, `SELECTORS`) e lettura di coordinate e tipologia
  nella pagina della struttura;
- convenzione su tasse e costi nei prezzi mostrati;
- numero di pagine necessarie per coprire il poligono (`max_pages`) e codici tipologia
  (`classification.type_ids`).

Se Booking cambia le pagine, il controllo qualità segnala "prezzi non leggibili" e i dati non entrano nello
storico finché i selettori non vengono aggiornati.

## Estensioni previste

Calendario eventi sovrapposto ai dati, prezzo suggerito, Metabase sullo stesso database.
