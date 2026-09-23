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