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
modules/         un modulo per fonte dati (insider, prezzi, news, 13F)
scoring/         aggregazione segnali → shortlist
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

> **Attenzione: è una coda di priorità, non un filtro esclusivo.** I CIK in
> lista vengono selezionati per primi, ma il budget `max_filings` viene
> comunque riempito con altri filer. Per processare **solo** i gestori
> indicati, il numero di CIK deve essere uguale o vicino a `max_filings`
> (nella config attuale: 3 CIK con `max_filings: 6`, per lasciare margine
> agli eventuali 13F-HR/A).

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