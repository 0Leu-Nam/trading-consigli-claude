# trading-consigli-claude

App di **supporto alle decisioni di investimento** (screening/segnalazione, NON trading automatico).
Monitora periodicamente fonti dati pubbliche e produce una shortlist di "candidati da approfondire".

## Principi vincolanti
- **Costo zero** totale (servizi free, nessuna carta di credito).
- **Mai fermo per inattività**: raccolta e analisi girano via GitHub Actions schedulato (cron), senza server sempre acceso.
- **Moduli a plugin**: ogni fonte dati è indipendente, stessa interfaccia (vedi `core/module_interface.py`).
- **Nessun segreto nel codice**: chiavi solo in `.env` / secrets.
- **Testabile in locale** prima di ogni deploy.

## Struttura
```
core/            contratto moduli, orchestratore, DB, config, reporting
modules/         un modulo per fonte dati (insider, prezzi, news, 13F, scoring)
scoring/         aggregazione segnali → shortlist (logica dentro modules/scoring)
dashboard/       generatore pagina statica (Fase 7)
data/            database SQLite versionato nel repo
tests/           pytest
```

## Setup locale
```
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env   # e valorizza solo ciò che serve
py -m pytest
```

## Roadmap (dettaglio in PROGRESS.md)
0. Scaffolding ✔(in corso) 1. Insider SEC EDGAR → 2. Cron GitHub Actions → 3. Prezzi/volumi → 4. News/sentiment → 5. 13F → 6. Scoring → 7. Dashboard → 8. Notifiche + hardening

## Come trovare il CIK di un gestore (per `filer_cik_filter`)

I 13F identificano le posizioni per **CUSIP**, non per ticker, e il modulo
ricava il ticker dal nome dell'emittente confrontandolo con la mappa EDGAR
`company_tickers.json`. I **CUSIP non risolti** restano tracciati in
`cusip_lookup` e vengono ritentati a ogni run.

`filer_cik_filter` serve per dare la precedenza ai 13F dei gestori scelti
invece di prendere i depositi più recenti.

> **La whitelist è esclusiva**: quando è valorizzata il modulo raccoglie
> **solo** i 13F dei CIK indicati. I CIK vengono usati come **filtro della
> ricerca EDGAR** (una richiesta per finestra di deposito), quindi i gestori
> scelti vengono raccolti anche se in elenco EFTS non sono i primi.
>
> `max_filings` è un **tetto per trimestre**, non un obiettivo da riempire:
> se i gestori in lista depositano meno documenti, gli slot restano liberi e
> non viene contato nessun altro filer. Non serve quindi tenere
> `max_filings` allineato alla lunghezza della whitelist.
>
> Per aggiungere volutamente altri filer, impostare
> `fill_remaining_with_generic: true` (default `false`): gli slot residui si
> riempiono con una ricerca generica e la nota del run dichiara
> `filer generici=N`.
>
> Il modulo segnala come errore (run classificato **`warning`**) due casi in
> cui un `ok` sarebbe un falso successo:
> - la whitelist è valorizzata ma nessun CIK ha depositato un 13F-HR nelle
>   finestre del run;
> - il tetto `max_filings` ha scartato il 13F di un gestore in lista
>   (un 13F-HR/A in più sullo stesso CIK invece è normale e non segnala).

### 1. Trovare il CIK
- **EDGAR full-text search**: https://efts.sec.gov/LATEST/search-index?q=NOME&forms=13F-HR
  (oppure la UI https://www.sec.gov/cgi-bin/srqsb?text=... , oppure
  "Search EDGAR" → impostare *Form Type* = **13F-HR** e digitare il nome).
  Ogni hit mostra `display_names` con il nome del filer e il suo CIK.
- **EDGAR company search**: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=NOME
- **Pagina del filing**: https://www.sec.gov/Archives/edgar/data/<CIK>/<accession>/

### 2. Usarlo in config.yaml
Il CIK si scrive a **10 cifre con zeri iniziali** (accettato anche senza zeri):

```yaml
institutional_holdings:
  filer_cik_filter: ["0001541617", "0002045724", "0001263508"]
  #                 0001541617 = Altimeter Capital Management, LP (verificato 2026-10-01)
  #                 0002045724 = Situational Awareness LP           (verificato 2026-10-01)
  #                 0001263508 = Baker Bros. Advisors LP           (verificato 2026-10-01)
```

Sono i tre gestori usati in produzione: piccoli e concentrati, scelti per
individuare mosse ad alta convinzione su titoli non ancora enormi. I CIK
vanno verificati sempre su EFTS (o con `https://data.sec.gov/submissions/CIK<CIK>.json`):
un CIK sbagliato non produce errori, semplicemente **zero righe in silenzio**.

### 3. Override manuale di un CUSIP non risolto
Se un CUSIP importante resta non risolto, si può inserire a mano la
corrispondenza:

```sql
UPDATE cusip_lookup
SET company_id = (SELECT id FROM companies WHERE ticker = 'XOM'),
    ticker = 'XOM', source = 'manual',
    resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE cusip = '30231G102';
```

La cache ha TTL (`cusip_ttl_days`, default 60): un override `manual` con
`resolved_at` fresco viene rispettato senza essere sovrascritto.

### 4. Quanto viene risolto, e cosa non viene risolto (misurato)
Su un campione reale di 8 filing 13F (329 CUSIP unici) il mapping per nome
risolve **31%** dei CUSIP. La parte non risolta è quasi sempre strutturale:

- **Fondi/ETF (~70% del residuo)**: `company_tickers.json` contiene 324 titoli
  su 10.431 con parole da fondo e zero voci per QQQ, DIMENSIONAL, GLOBAL X.
  Le posizioni in ETF restano non risolte, cosa per lo screening azionario è in
  gran parte desiderabile.
- **Classi multiple**: chi ha più ticker con lo stesso titolo (Alphabet
  GOOG/GOOGL, AT&T T/T-PA, JPMorgan con 9 ticker) resta non risolto per scelta:
  la 13F riporta la classe in `titleOfClass`, EDGAR non la riporta, quindi
  attribuire un ticker sarebbe un'invenzione.
- **Abbreviazioni in stile 13F** (`APPLIED MATLS`, `CISCO SYS`, `EXXON MOBIL`):
  recuperabili con un dizionario, non ancora implementato.

Se un CUSIP ti interessa anche se è in una di queste categorie, l'override
manuale qui sopra è la via prevista.

## Scoring: come si calcola il punteggio (Fase 6)

Il modulo `scoring` non raccoglie dati: legge le tabelle degli altri 4 moduli e
combin i segnali in un punteggio per ticker, scritto in `signals`.

### Dove finiscono i numeri

Tutti i pesi e le finestre sono in `config.yaml`, sezione `modules.scoring`.
Nessun numero è nel codice: cambiando un peso lanciando il run cambia lo score
(e c'è un test che lo verifica).

| peso | cosa lo genera | cosa NON significa |
|---|---|---|
| `insider_open_market_buy` +30 | `transaction_type = 'P'`, `is_open_market = 1`, sopra `min_value_usd` nella finestra | non è un giudizio sull'azienda: è un segnale sul fatto che qualcuno ha comprato per conto proprio |
| `insider_open_market_sell` −20 | `transaction_type = 'S'` open-market sopra soglia | i tipi `M`, `F`, `A`, `C`, `D`, `G` non sono mappati: sono esercizi di opzioni, assegnazioni e donazioni, non convinzione |
| `price_volume_spike` +15 | `vol_vs_avg_20 >= vol_spike_mult` | è **volume**, non direzione del prezzo: uno spike può essere su un rialzo o su un crollo |
| `price_move_up` +10 | rendimento a 5 giorni `>= move_5d_pct`, col segno | la colonna `abs_return_5d` contiene il rendimento con segno (misurato: min −0.85, max +1.74). Un ribasso non genera contributo positivo: da solo, un −30% e un +30% hanno lo stesso valore assoluto e significati opposti |
| `news_sentiment_positive` +20 | media del sentiment `>= positive_gte` su almeno `min_articles` articoli | `min_articles` serve perché il sentiment è rumore: 711 articoli su 1082 sono `neutral` e la media di un solo titolo (+1.0) è più estrema di una diffusa (+0.2) |
| `news_sentiment_negative` −15 | media `>= negative_lte` con lo stesso minimo di articoli | |
| `institutional_new_position` +25 | posizione aperta nel trimestre da un gestore in `filer_cik_filter` | il 13F ha 45–90 giorni di lag: conferma una convinzione passata, non anticipa un movimento |
| `institutional_increase` +15 | azioni aumentate rispetto al trimestre precedente | una posizione chiusa non genera contributo: la sua assenza è rumore, non una decisione di vendita |
| `institutional_multiple` +5 | due o più gestori whitelistati che aprono o aumentano la stessa posizione | |

Il segnale istituzionale è calcolato qui e non letto da
`institutional_holdings.shares_delta`: quella colonna è NULL su tutto il DB,
perché il modulo 13F confronta con il trimestre precedente *dentro lo stesso
run* e con `quarters_back: 2` il primo trimestre non trova un precedente.

### Un modulo senza dati non è un punteggio negativo

Misurato sul DB reale: **nessuna company ha tutti e 4 i segnali**, 122 ne
hanno 2 e 15 ne hanno 3. L'assenza di dati è quindi il caso normale.

Perciò un modulo che non parla **contribuisce 0 e non penalizza**. Ogni riga
dichiara quanto ha contribuito:

```
MU   +45.0  score +45.0 da 3 contributi, copertura 1/4 (institutional_holdings)
KLAC +35.0  score +35.0 da 2 contributi, copertura 2/4 (institutional_holdings,price_screener)
```

Uno score di 60 su 2 moduli non è confrontabile con uno di 60 su 4 senza
leggere la copertura, ed è per questo che è scritta nella riga.

`min_signals` (default 2) tiene fuori dalla shortlist i punteggi costruiti su
una sola fonte. **La riga resta in tabella**: la shortlist è un filtro di
lettura, non di scrittura.

### Ogni finestra è dichiarata

`windows` in config, per modulo: `insider` 7 giorni, `news` 14, `price` 5,
`institutional` un trimestre con `max_age_days`. L'istituzionale è
trimestrale: paragonarlo a una finestra settimanale premieria posizioni
vecchie come se fossero notizie. Un 13F più vecchio di `max_age_days` non
contribuisce e **non è un errore**: è il segnale che non c'è più.

L'età del dato finisce nella description della riga, così in dashboard è
visibile che quel segnale ha 45+ giorni.

### Come è tracciabile

Nessuna migrazione di schema: si riusa il vincolo
`UNIQUE (company_id, module_key, signal_type, signal_date)` già presente.

- **una riga per contributo**, con il suo peso e il perché:
  `insider_open_market_buy`, `magnitude=30`,
  `description="acquisto open-market 27.5M da 2 insider"`
- **una riga di sintesi** per ticker: `module_key='scoring'`,
  `signal_type='composite'`, `magnitude` = totale, `description` = riepilogo
  dei contributi e della copertura.

La dashboard (Fase 7) mostra il totale e i dettagli con due query, senza
parsing di JSON e senza ricalcolare nulla.

I contributi con lo stesso tipo sono aggregati in una riga (pesi sommati,
descrizioni concatenate): altrimenti due gestori che aprono la stessa
posizione avrebbero la stessa chiave e il secondo verrebbe scartato in
silenzio.

### Idempotenza

`signal_date` è l'ultima barra presente in `price_snapshots`, **non**
`date.today()`: un ricalcolo il giorno dopo sullo stesso dataset produce la
stessa chiave e non scrive nulla. Con nuovi dati di prezzo la chiave cambia e
nasce una riga nuova, come negli altri moduli.

`INSERT OR IGNORE` di default (un run ripetuto non altera nulla). Con
`recalc: true` la sovrascrittura diventa esplicita.

### Errori

Un extractor che solleva non ferma gli altri: gli altri moduli contribuiscono,
l'errore resta in `errors[]` e il run viene classificato `warning`. Un'assenza
di dati non genera errori. `note` riporta quanti ticker sono stati valutati,
quanti in shortlist e quanti ignorati da watchlist.

### Segnali contrastanti

Se un ticker ha **contributi positivi e negativi entrambi sopra
`conflict_min_weight`** (default 10), la riga di sintesi lo dichiara:

```
CBRS +35.0  score +35.0 da 3 contributi, copertura 2/4
            (insider_trading,institutional_holdings);
            segnali contrastanti: + institutional_new_position vs - insider_open_market_sell
```

Il caso reale: 36 insider in vendita open-market per 87.7M (`-20`) e una nuova
posizione di Altimeter (`+25 +25`, piu' `+5` multi-gestore) totalizzano un
`+35` che sembrava pieno. Metà dei contributi tirava dalla parte opposta e
niente nel numero lo diceva.

**Il punteggio non cambia e la vendita non è bloccata**: `insider_open_market_sell`
resta un peso negativo normale da -20. Quello che cambia è che il conflitto è
dichiarato dove si legge il risultato. La ragione è che uno `+35` costruito
con due fonti d'accordo e uno `+35` costruito annullando un disaccordo non
sono lo stesso segnale, e la loro somma numerica è identica.

La soglia serve a non dichiarare conflitto il rumore: `institutional_multiple`
da `+5` contro una vendita da `-20` non è un disaccordo fra due fonti, e senza
soglia l'etichetta finirebbe su quasi tutti i ticker con più di due moduli.
`note` riporta anche il conteggio dei ticker con conflitto, così è visibile
prima di aprire le righe: nel run del 2026-10-03 erano 6 su 145, di cui 2 in
shortlist (CBRS e CRWV).

### watchlist

I ticker con `status = 'ignore'` non ricevono punteggio e non compaiono in
shortlist, anche con score alto. Il conteggio finisce in `note`.