# PROGRESS.md — Registro di continuità tra sessioni

## Stato attuale
- Fase in corso: 2 — Scheduler automatico GitHub Actions (cron, senza server)
- Percentuale completamento fase: 80% (implementazione e test locale fatti; resta da verificare il funzionamento autonomo su GitHub per 3-5 giorni)

## Ultima sessione conclusa
- Fase conclusa: 1 — Primo modulo dati: insider trading SEC EDGAR (100%, verificata il 2026-09-23)
- File creati in Fase 2:
  - `.github/workflows/pipeline.yml` (cron `0 13,21 * * *` + `workflow_dispatch`, job `test` con pytest, job `run` con `run-all` + auto-commit di `data/` via GITHUB_TOKEN)
- File modificati in Fase 2:
  - `.github/workflows/pipeline.yml` (aggiornate le azioni a runtime **Node 24**: `actions/checkout@v5` e `actions/setup-python@v6`, in entrambi i job — elimina l'avviso di deprecazione Node 20)
  - `modules/insider_trading/module.py` (fallback User-Agent: `ctx.env(...) or DEFAULT_USER_AGENT` — un secret vuoto non produce più un UA vuoto che SEC rifiuterebbe con 403)
  - `.gitignore` (aggiunti `data/*-wal` e `data/*-shm`)
  - `.env.example` (rimossa `STORAGE_PAT`: l'auto-commit usa il GITHUB_TOKEN incorporato, nessun PAT necessario)

## Problemi riscontrati e decisioni prese
- Primo run GitHub Actions: job `Test (pytest)` verde ma con avviso "Node.js 20 is deprecated" su `checkout@v4`/`setup-python@v5` (Node 20 in EOL ad aprile 2026, runner su Node 24 da giugno 2026) → aggiornate le azioni a `actions/checkout@v5` + `actions/setup-python@v6` (runtime Node 24, input `python-version` invariato). Nessuna modifica di comportamento.
- PyYAML (YAML 1.1) parsa `on:` come booleano → falso errore di validazione; GitHub usa un parser YAML 1.2 per cui `on:` è il trigger corretto. Nessuna modifica: pattern standard di ogni workflow.
- Auto-commit: invece del PAT (piano originale) si usa il **GITHUB_TOKEN dedicato** con `permissions: contents: write` → zero secret, zero carta, e i commit del bot non ri-innescano il workflow (GitHub li ignora come trigger) quindi nessun loop. Il "repo sempre attivo" per via dei commit automatici tiene anche i cron dei repo privati fuori dalla disattivazione per inattività (60gg).
- `concurrency` impostato con `cancel-in-progress: false`: i 2 run/giorno non si sovrappongono mai (controllo su WAL e auto-commit).
- Possibile rallentamento/blocco del job `run` sul primo esecuzione (250 filing × 2 richieste SEC): se il run su GitHub supera i ~10-15 minuti è segno di rate-limit SEC sugli IP GitHub; in quel caso si può abbassare `max_filings` in config.yaml.

## Criteri di successo Fase 2
- ✅ Workflow creato e sintatticamente valido; pytest 20/20 in locale; la sequenza dei passi (checkout→install→run-all→commit) è stata eseguita manualmente in locale con esito ok.
- ⏳ DA VERIFICARE SUL TUO ACCOUNT GITHUB: push del repo **pubblico**, poi osservare per **3-5 giorni** che il cron parta in autonomia (tab Actions + `run_log` crescente) anche senza aprire nulla.

## Prossimo step esatto
1. Creare il repo pubblico su GitHub e pushare (**serve una tua azione su GitHub**, non ho i tuoi crediti): 
   - `gh repo create trading-consigli-claude --public --source . --remote origin --push`
   - oppure: creare il repo vuoto sul sito, poi `git remote add origin <url>` e `git push -u origin main`.
2. Aprire il tab Actions del repo: verificare a mano (Run workflow) che `test` e `run` completino ok e che appaia un commit `data: aggiornamento pipeline …`.
3. Se consigliato, impostare il secret `SEC_EDGAR_USER_AGENT` (nostro nome + email) nel repo — più conforme alla policy fair-access di SEC.
4. Dopo 3-5 giorni controllare su PROGRESS che i `run_log` siano cresciuti ogni giorno senza un click umano → poi aggiornare PROGRESS.md a "Fase 2 = 100%" e passare alla Fase 3 (modulo `price_screener`, OHLCV via yfinance + fallback Stooq, anomalie prezzo/volume in `price_snapshots`).