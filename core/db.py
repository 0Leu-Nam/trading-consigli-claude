"""Accesso al database SQLite condiviso.

Un file ``data/app.db`` persistente gestito come unico storage.
Convenzioni per evitare conflitti tra moduli:
- l'orchestratore esegue i moduli in sequenza (single-writer);
- ogni modulo scrive SOLO nelle proprie tabelle + ``companies`` (upsert);
- ``module_state`` contiene il watermark di ogni modulo (riparte da dove era rimasto);
- idempotenza garantita da chiavi naturali UNIQUE + INSERT OR IGNORE.
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

EXPECTED_TABLES = {
    "companies",
    "insider_transactions",
    "institutional_holdings",
    "price_snapshots",
    "news_events",
    "signals",
    "watchlist",
    "run_log",
    "module_state",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL UNIQUE,
    name        TEXT,
    cik         TEXT,
    sic         TEXT,
    exchange    TEXT,
    industry    TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insider_transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    accession         TEXT NOT NULL,
    row_no            INTEGER NOT NULL DEFAULT 0,
    filing_date       TEXT NOT NULL,
    transaction_date  TEXT,
    insider_name      TEXT,
    insider_title     TEXT,
    transaction_type  TEXT,
    shares            INTEGER,
    price_per_share   REAL,
    value_usd         INTEGER,
    holdings_after    INTEGER,
    is_open_market    INTEGER NOT NULL DEFAULT 0,
    url               TEXT,
    UNIQUE (accession, row_no)
);

CREATE TABLE IF NOT EXISTS institutional_holdings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    filing_quarter TEXT NOT NULL,
    filing_date    TEXT,
    filer_name     TEXT,
    cik            TEXT,
    shares         INTEGER,
    value_usd      INTEGER,
    shares_delta   INTEGER,
    UNIQUE (filing_quarter, filer_name, company_id)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    date         TEXT NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       INTEGER,
    abs_return_1d REAL,
    abs_return_5d REAL,
    vol_vs_avg_20 REAL,
    UNIQUE (symbol, date)
);

CREATE TABLE IF NOT EXISTS news_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    uuid            TEXT NOT NULL UNIQUE,
    published_at    TEXT NOT NULL,
    source          TEXT,
    title           TEXT,
    url             TEXT,
    sentiment_score REAL,
    sentiment_label TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    generated_at TEXT NOT NULL,
    signal_date  TEXT NOT NULL,
    module_key   TEXT NOT NULL,
    signal_type  TEXT NOT NULL,
    magnitude    REAL,
    direction    INTEGER,
    description  TEXT,
    UNIQUE (company_id, module_key, signal_type, signal_date)
);

CREATE TABLE IF NOT EXISTS watchlist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
    note       TEXT,
    status     TEXT NOT NULL DEFAULT 'watch' CHECK (status IN ('watch', 'ignore'))
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    modules_run TEXT,
    errors      TEXT
);

CREATE TABLE IF NOT EXISTS module_state (
    module_key        TEXT PRIMARY KEY,
    last_run_at       TEXT,
    last_processed_id TEXT,
    state_json        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_cik ON companies (cik) WHERE cik IS NOT NULL;
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Apre una connessione SQLite con row factory, FK attive e WAL."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(db_path: Path | str) -> None:
    """Crea (idempotente) il DB, la directory padre e tutte le tabelle."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(_SCHEMA)


def table_names(db_path: Path | str) -> set[str]:
    """Nomi delle tabelle presenti nel DB (per test e comando `status`)."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return {r["name"] for r in rows}


def db_exists(db_path: Path | str) -> bool:
    return Path(db_path).exists()


# ── Helper condivisi usati da orchestratore e moduli ──────────────────────────


def utcnow_iso() -> str:
    """Timestamp ISO-8601 UTC (secondi), usato per run_log e watermark."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_company_id(conn, ticker: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    return row["id"] if row else None


def upsert_company(
    conn,
    ticker: str,
    *,
    name: str | None = None,
    cik: str | None = None,
    sic: str | None = None,
    exchange: str | None = None,
    industry: str | None = None,
) -> int:
    """Inserisce o aggiorna la company e torna il suo id (modalità idempotente)."""
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO companies (ticker, name, cik, sic, exchange, industry, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker) DO UPDATE SET
            name        = COALESCE(excluded.name,        companies.name),
            cik         = COALESCE(excluded.cik,         companies.cik),
            sic         = COALESCE(excluded.sic,         companies.sic),
            exchange    = COALESCE(excluded.exchange,    companies.exchange),
            industry    = COALESCE(excluded.industry,    companies.industry),
            updated_at  = excluded.updated_at
        """,
        (ticker, name, cik, sic, exchange, industry, now),
    )
    return get_company_id(conn, ticker)


def get_module_state(conn, module_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM module_state WHERE module_key = ?", (module_key,)
    ).fetchone()
    return dict(row) if row else None


def upsert_module_state(
    conn,
    module_key: str,
    *,
    last_run_at: str,
    last_processed_id: str | None = None,
    state_json: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO module_state (module_key, last_run_at, last_processed_id, state_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (module_key) DO UPDATE SET
            last_run_at       = excluded.last_run_at,
            last_processed_id = COALESCE(excluded.last_processed_id, module_state.last_processed_id),
            state_json        = COALESCE(excluded.state_json,        module_state.state_json)
        """,
        (module_key, last_run_at, last_processed_id, state_json),
    )


def start_run_log(conn, started_at: str, modules: list[str]) -> int:
    """Registra l'inizio di un run. Torna l'id della riga run_log."""
    import json

    cur = conn.execute(
        "INSERT INTO run_log (started_at, status, modules_run) VALUES (?, 'running', ?)",
        (started_at, json.dumps(modules)),
    )
    return cur.lastrowid


def finish_run_log(
    conn,
    run_id: int,
    *,
    status: str,
    finished_at: str,
    errors: list[str] | None = None,
) -> None:
    import json

    conn.execute(
        "UPDATE run_log SET status = ?, finished_at = ?, errors = ? WHERE id = ?",
        (status, finished_at, json.dumps(errors or []), run_id),
    )