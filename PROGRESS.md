# PROGRESS.md — Registro di continuità tra sessioni

## Stato attuale
- Fase in corso: 3 — Modulo price_screener (anomalie prezzo/volume)
- Percentuale completamento fase: 90% (implementazione + test verde 48/48 + run reale su universe completa ok il 2026-09-24). Resta: osservazione dell'esito del primo cron su GitHub, poi chiusura formale Fase 2 = 100% e validazione incrociata Fase 3.

## Ultima sessione conclusa
- Sessione 2026-09-24: Fase 3 implementata e validata in locale.
- File creati in Fase 3:
  - `data/sp500.txt` (503 ticker S&P 500, generati da Wikipedia il 2026-09-24; per rigenerarli: venv con `lxml` + `pandas.read_html` + User-Agent browser — Wikipedia risponde 403 senza UA)
  - `modules/price_screener/data_sources.py` (fetch OHLCV via `yfinance` in batch, alias Yahoo per azioni di classe `BRK.B→BRK-B`, fallback Stooq CSV se `STOOQ_API_KEY` presente; errore irrecuperabile `PriceSourceError`)
  - `modules/price_screener/indicators.py` (funzioni pure: `pct_return` 1d/5d, `volume_ratio` su finestra 20, `build_snapshot_rows`)
  - `modules/price_screener/module.py` (`resolve_universe`: `sp500`/`file:<path>`/lista inline + cap `max_symbols`; upsert companies + INSERT OR IGNORE in price_snapshots)
  - `tests/test_price_indicators.py`, `tests/test_price_module.py`, `tests/test_price_sources.py`
- File modificati in Fase 3:
  - `pyproject.toml` (aggiunta dipendenza `yfinance>=0.2.55`)
  - `config.yaml` (`price_screener.enabled: true`, parametri `period_days: 30`, `history_days: 25`, `max_symbols: 500`, `universe: file:data/sp500.txt`; sostituito il vecchio `lookback_days: 5` non più usato)
  - `tests/test_config.py` (ora i moduli attesi attivi sono `insider_trading` + `price_screener`)
  - `.env.example` (aggiunto `STOOQ_API_KEY`)

## Problemi riscontrati e decisioni prese
- **Azioni di classe su Yahoo**: Yahoo vuole il trattino (`BRK-B`) e rifiuta il punto (`BRK.B`); yfinance inoltre scarta dai risultati i simboli che non risolve → nel batch serviva passare a `download` l'alias `ticker.replace(".", "-")` e ri-mappare i nomi originali in output. Risolto con `_yahoo_alias()` + `_extract_ticker()` resiliente (ticker assente = lista vuota, non eccezione KeyError).
- **Wikipedia 403 con l'UA di pandas** → scaricata la pagina con `requests` + User-Agent browser e poi `pandas.read_html`; `data/sp500.txt` è un file versionato, la rigenerazione non è un'operazione di runtime.
- **Stooq: senza chiave risponde con challenge JavaScript** ("This site requires JavaScript...") da richieste non-browser → Stooq è usabile SOLO con `STOOQ_API_KEY` gratuita (CAPTCHA) come fallback per i ticker vuoti di yfinance; la risposta di quota si riconosceva dal testo "Exceeded the daily hits limit".
- **Fase 2 (chiusa sul campo)**: il workflow Actions è verde end-to-end (test + raccolta + auto-commit), fix deprecazione Node 20 fatto (checkout v5 / setup-python v6) e il job `run` ha completato in verde con 250 filing (~6-9 min, normale). Resta solo la verifica a campione dell'autonomia 3-5 giorni.
- Avviso "ubuntu-latest migrerà a Ubuntu 26 dal 19/10/2026" (notice, non errore): scelta A applicata, nessuna modifica (i job usano solo pip/pytest/script, la migrazione non li tocca). Opzione B (pin `ubuntu-24.04`) disponibile se in futuro si vuole un ambiente 100% bloccato.
- PyYAML 1.1 parsa `on:` come booleano → falso errore di validazione locale; GitHub usa YAML 1.2, `on:` è corretto. Nessuna azione.
- Auto-commit con GITHUB_TOKEN dedicato (`contents: write`): zero secret/carta; i commit bot non ri-innescano il workflow (nessun loop) e tengono il repo attivo (cron non disattivato a 60gg nei repo privati).
- `concurrency` con `cancel-in-progress: false`: i 2 run/giorno non si sovrappongono (sicurezza WAL e auto-commit).
- Primo run CI lento (~6-9 min) per 250 filing × 2 richieste SEC: normale; se in futuro i run CI di SEC dovessero superare i 10-15 min (rate limit sugli IP GitHub), abbassare `max_filings`.

## Criteri di successo Fase 3 (locale, già verificati il 2026-09-24)
- ✅ pytest: 48/48 verdi (indicatori, modulo con rete mockata, parsing/normalizzazione sorgenti).
- ✅ Run reale su universe completa (500/503 ticker per `max_symbols`): 12.500 righe in `price_snapshots`, finestra 2026-08-18 → 2026-09-23, 0 ticker senza dati.
- ✅ Idempotenza: secondo run = 0 righe nuove (UNIQUE symbol+date).
- ✅ Anomalie interrogabili: query su `vol_vs_avg_20`/`abs_return_5d` del giorno più recente restituisce spike sensati (es. WBD vol_ratio 3.72, MCD -4.8% in 1d).
- ✅ Azioni di classe gestite: BRK.B e BF.B presenti con dati.
- ⏳ DA VERIFICARE SUL TUO ACCOUNT GITHUB: confondere il nuovo `run` CI (una esecuzione con entrambi i moduli) → poi guardare per 3-5 giorni l'autonomia del cron; a quel punto PROGRESS → "Fase 2 = 100%" e Fase 3 → 100% (validata in produzione).

## Prossimo step esatto
1. Push delle modifiche appena fatte (file di Fase 3 + test + `data/app.db` con le 12.500 righe nuove) tramite commit esplicito, poi osservare il prossimo run del cron su GitHub per confermare che `price_screener` giri anche lì (il job stampa "Moduli attivi: insider_trading, price_screener").
2. Dopo 3-5 giorni di cron autonomo: aggiornare PROGRESS.md a "Fase 2 = 100%" e "Fase 3 = 100%".
3. Avviare la **Fase 4 — modulo news_sentiment**: fonti gratuite senza chiave nella fase iniziale (es. RSS Finviz) + Marketaux con token opzionale; campo `sentiment_label/score` già nello schema `news_events`.