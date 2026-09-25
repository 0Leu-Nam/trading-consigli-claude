# PROGRESS.md — Registro di continuità tra sessioni

## Stato attuale
- Fase in corso: 4 — Modulo news_sentiment (notizie + sentiment)
- Percentuale completamento fase: 95% (codice + test 76/76 + prova reale locale ok il 2026-09-24; **`enabled: false` per scelta: l'attivazione in produzione avviene solo dopo la chiusura formale di Fase 2/3 = 100% in questo file**).
- Fase 3: 90% (implementata e validata; in parallelo alla osservazione cron di Fase 2).

## Ultima sessione conclusa
- Sessione 2026-09-24 (sera): Fase 4 implementata e validata in locale, produzione OFF.
- File creati in Fase 4:
  - `modules/news_sentiment/sentiment.py` (lessico EN + `sentiment_score` [-1,1] e `sentiment_label` su soglia, funzioni pure)
  - `modules/news_sentiment/data_sources.py` (`NewsItem`, `NewsSourceError`, RSS per-ticker Yahoo Finance gratuita, API Marketaux con sentiment; `synthesize_uuid` con base **url** e fallback titolo; parsing XML/JSON con scarto degli item malformati)
  - `modules/news_sentiment/module.py` (selezione prioritaria insider recente → watchlist → resto universe fino a `max_symbols`; loop fonti isolato per-ticker; status ok/error con semantica esplicita)
  - `tests/test_news_sentiment.py`, `tests/test_news_sources.py`, `tests/test_news_module.py`
- File modificati in Fase 4:
  - `config.yaml` (sezione `news_sentiment` con parametri e **`enabled: false`**; `universe: file:data/sp500.txt`; `sources: [marketaux, yahoo_finance_rss]`)

## Verifiche di Fase 4 (locale, il 2026-09-24)
- ✅ pytest 76/76 verdi.
- ✅ Prova reale su rete (DB temporaneo): 4 ticker coperti, 60 articoli da Yahoo RSS (nessuna chiave), 57 inseriti, secondo run 0 (idempotente), sentiment label presenti.
- ✅ Test espliciti di isolamento fonti: Yahoo RSS che fallisce (HTTP/XML/network) non blocca Marketaux né il run; Marketaux giù non blocca Yahoo; feed malformato per 1 ticker lascia intatti gli altri.
- ✅ Semantica status richiesta in seduta: fonti ok ma zero notizie nuove → `status: ok` con nota "nessuna notizia nuova (fonti ok)"; solo fallimento tecnico di TUTTE le fonti → `status: error`.
- ✅ UUID sintetico Marketaux senza guid: basato su `url` (se c'è), fallback `title`; item senza url né titolo → scartato.
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
- **Finviz non ha un feed RSS per-ticker**: `news.ashx?v=3&t=TICKER` è HTML ("Stocks News" generale), `rss.ashx` è 404 → fonte senza chiave scelta: **RSS per-ticker di Yahoo Finance** (`feeds.finance.yahoo.com/rss/2.0/headline?s=...`) con `guid` come uuid e normalizzazione RFC822→ISO.
- **Marketaux free plan**: 100 richieste/giorno e 3 articoli/richiesta → 1 richiesta per simbolo, cap 40 ticker/run = max 80/giorno con 2 cron, dentro la quota. Se in futuro la quota si esaurisse: ridurre `max_symbols` o attivare Marketaux solo sul tier prioritario.
- **Ridondanza uuid**: lo stesso articolo può comparire nei feed RSS di due ticker → UNIQUE(uuid) + INSERT OR IGNORE evitano i duplicati (vince il primo simbolo processato; ok per lo screening).
- **Isolamento fonti** (richiesta esplicita di test): semantica di `status` definita nel modulo — ok anche con fonti ok e nessuna notizia nuova (annotato in `note`); `error` solo quando TUTTE le fonti falliscono tecnicamente e 0 righe inserite; degradazioni parziali vanno in `note`, non in `errors` (per non rendere rosso il cron ad ogni 404 singolo).
- **UUID sintetico** (richiesta esplicita): base = `url` dell'articolo, fallback `title`, `uuid5(NAMESPACE_URL, base)` deterministico.
- **Divergenza main locale/remota (2026-09-24)**: dopo la Fase 3 il push è stato rifiutato ("fetch first") perché il bot di Actions aveva già auto-committato `data/app.db` sul remote. Il conflitto è stato risolto dentro `git pull --rebase` con `git checkout --theirs data/app.db`; nota di cautela: **nel rebase "theirs" è il commit rigiocato (il nostro)**, non l'upstream come nel merge → è stato conservato il DB LOCALE (con le 12.500 righe prezzo), non quello del bot. Esito comunque sano: origin ha ricevuto il DB locale e i run CI successivi lo integrano (idempotenza INSERT OR IGNORE), senza dati persi né duplicati.
- **Rumore line endings su Windows**: warning "LF will be replaced by CRLF" a ogni `git add` → aggiunto `.gitattributes` (`* text=auto`, `.db`/`-wal`/`-shm` come `binary`) per normalizzare e silenziare. Nessun cambio di comportamento.
- **Azioni di classe su Yahoo**: Yahoo vuole il trattino (`BRK-B`) e rifiuta il punto (`BRK.B`); yfinance inoltre scarta dai risultati i simboli che non risolve → nel batch serviva passare a `download` l'alias `ticker.replace(".", "-")` e ri-mappare i nomi originali in output. Risolto con `_yahoo_alias()` + `_extract_ticker()` resiliente (ticker assente = lista vuota, non eccezione KeyError).
- **Wikipedia 403 con l'UA di pandas** → scaricata la pagina con `requests` + User-Agent browser e poi `pandas.read_html`; `data/sp500.txt` è un file versionato, la rigenerazione non è un'operazione di runtime.
- **Stooq: senza chiave risponde con challenge JavaScript** ("This site requires JavaScript...") da richieste non-browser → Stooq è usabile SOLO con `STOOQ_API_KEY` gratuita (CAPTCHA) come fallback per i ticker vuoti di yfinance; la risposta di quota si riconosceva dal testo "Exceeded the daily hits limit".
- **Fase 2 (chiusa sul campo)**: il workflow Actions è verde end-to-end (test + raccolta + auto-commit), fix deprecazione Node 20 fatto (checkout v5 / setup-python v6) e il job `run` ha completato in verde con 250 filing (~6-9 min, normale). Resta solo la verifica a campione dell'autonomia 3-5 giorni.
- Avviso "ubuntu-latest migrerà a Ubuntu 26 dal 19/10/2026" (notice, non errore): scelta A applicata, nessuna modifica (i job usano solo pip/pytest/script, la migrazione non li tocca). Opzione B (pin `ubuntu-24.04`) disponibile se in futuro si vuole un ambiente 100% bloccato.
- PyYAML 1.1 parsa `on:` come booleano → falso errore di validazione locale; GitHub usa YAML 1.2, `on:` è corretto. Nessuna azione.
- Auto-commit con GITHUB_TOKEN dedicato (`contents: write`): zero secret/carta; i commit bot non ri-innescano il workflow (nessun loop) e tengono il repo attivo (cron non disattivato a 60gg nei repo privati).
- `concurrency` con `cancel-in-progress: false`: i 2 run/giorno non si sovrappongono (sicurezza WAL e auto-commit).
- **Ritardi cron osservati (2026-09-25)**: due run consecutivi dello schedule hanno avuto ritardi rilevanti rispetto all'orario previsto — il run atteso alle 13:00 UTC è partito con ~4h44min di ritardo, quello atteso alle 21:00 UTC con ~2h45min. Ipotesi: congestione dei runner GitHub Actions nella fascia oraria "tonda" (:00) condivisa da migliaia di workflow. **Decisione**: schedule spostato a minuto non standard `17` (`17 13,21 * * *` → 13:17 e 21:17 UTC) per uscire dal picco. Verifica attesa: i prossimi run autopartiti dovrebbero iniziare entro pochi minuti dall'orario esatto; la voce verrà aggiornata (o rivista) in base all'esito. Nessun dispatch manuale di controllo: l'obiettivo è osservare proprio il comportamento del cron automatico.
- Primo run CI lento (~6-9 min) per 250 filing × 2 richieste SEC: normale; se in futuro i run CI di SEC dovessero superare i 10-15 min (rate limit sugli IP GitHub), abbassare `max_filings`.

## Criteri di successo Fase 3 (locale, già verificati il 2026-09-24)
- ✅ pytest: 48/48 verdi (indicatori, modulo con rete mockata, parsing/normalizzazione sorgenti).
- ✅ Run reale su universe completa (500/503 ticker per `max_symbols`): 12.500 righe in `price_snapshots`, finestra 2026-08-18 → 2026-09-23, 0 ticker senza dati.
- ✅ Idempotenza: secondo run = 0 righe nuove (UNIQUE symbol+date).
- ✅ Anomalie interrogabili: query su `vol_vs_avg_20`/`abs_return_5d` del giorno più recente restituisce spike sensati (es. WBD vol_ratio 3.72, MCD -4.8% in 1d).
- ✅ Azioni di classe gestite: BRK.B e BF.B presenti con dati.
- ⏳ DA VERIFICARE SUL TUO ACCOUNT GITHUB: confondere il nuovo `run` CI (una esecuzione con entrambi i moduli) → poi guardare per 3-5 giorni l'autonomia del cron; a quel punto PROGRESS → "Fase 2 = 100%" e Fase 3 → 100% (validata in produzione).

## Prossimo step esatto
1. Push delle modifiche di Fase 4 (codice + test; `news_sentiment.enabled: false`) via commit esplicito.
2. Continuare l'osservazione dei 3-5 giorni di cron autonomo (Fase 2) con `price_screener` attivo (Fase 3).
3. Alla scadenza: aggiornare PROGRESS.md a "Fase 2 = 100%" e "Fase 3 = 100%", poi **attivare Fase 4** con una sola modifica: `news_sentiment.enabled: true` + aggiornare `tests/test_config.py` (3 moduli attivi) + run reale + push.
4. Avviare la **Fase 5 — institutional_holdings (13F trimestrale, SEC EDGAR)**: riusa il pattern di Fase 1 (EFTS `forms=13F-HR`), le tabelle dello schema esistono già.

Nota per Fase 7 (da non dimenticare): dividere in 7a (dashboard tabellare pura) e 7b (aggiunta sezione discorsiva via template, non LLM, per non rompere la tabella già funzionante). Vedi conversazione Claude del 24/09 per dettaglio completo del prompt.