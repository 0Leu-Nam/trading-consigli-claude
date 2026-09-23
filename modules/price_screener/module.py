"""Modulo price_screener: anomalie prezzo/volume (screening, nessun ordine).

Scarica lo storico OHLCV della universe, calcola rendimenti a 1 e 5 giorni e
il volume relativo alla media a 20 sessioni, poi salva tutto in
``price_snapshots``. Idempotente grazie a UNIQUE(symbol, date) + INSERT OR
IGNORE: un secondo run aggiunge solo le giornate nuove.
"""

import logging
from pathlib import Path

from core import db
from core.module_interface import ModuleInterface, ModuleResult, RunContext
from modules.price_screener import data_sources, indicators

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
UNIVERSE_FILE = PROJECT_ROOT / "data" / "sp500.txt"


def resolve_universe(module_config: dict) -> list[str]:
    """Risolve il parametro `universe`: ``sp500``, ``file:<path>`` o lista inline."""
    universe = module_config.get("universe")
    if isinstance(universe, list):
        return [
            str(s).strip().upper()
            for s in universe
            if str(s).strip() and not str(s).lstrip().startswith("#")
        ]
    if isinstance(universe, str) and universe.strip().lower() == "sp500":
        path: Path = UNIVERSE_FILE
    elif isinstance(universe, str) and universe.startswith("file:"):
        path = Path(universe[5:])
        if not path.is_absolute():
            path = PROJECT_ROOT / path
    else:
        raise ValueError("universe non valido: usa 'sp500', 'file:<path>' o una lista")
    if not path.exists():
        raise ValueError(f"file universe non trovato: {path}")
    return [
        line.strip().upper()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class Module(ModuleInterface):
    key = "price_screener"
    display_name = "Anomalie prezzo/volume"

    def run(self, ctx: RunContext) -> ModuleResult:
        period_days = int(ctx.get("period_days", 30))
        history_days = int(ctx.get("history_days", 25))
        max_symbols = int(ctx.get("max_symbols", 500))
        stooq_key = ctx.env("STOOQ_API_KEY") or None

        try:
            tickers = resolve_universe(ctx.module_config)
        except ValueError as exc:
            return ModuleResult(module_key=self.key, status="error", errors=[str(exc)])

        tickers = tickers[:max_symbols]
        if not tickers:
            return ModuleResult(module_key=self.key, status="skipped", note="universe vuota")

        try:
            bars_map, no_data = data_sources.fetch_history(tickers, days=period_days, stooq_key=stooq_key)
        except data_sources.PriceSourceError as exc:
            return ModuleResult(module_key=self.key, status="error", errors=[str(exc)])

        written = 0
        updated = 0
        for ticker, bars in bars_map.items():
            if not bars:
                continue
            company_id = db.upsert_company(ctx.conn, ticker)
            for row in indicators.build_snapshot_rows(bars, history_days):
                cur = ctx.conn.execute(
                    """
                    INSERT OR IGNORE INTO price_snapshots
                        (company_id, symbol, date, open, high, low, close, volume,
                         abs_return_1d, abs_return_5d, vol_vs_avg_20)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company_id,
                        ticker,
                        row["date"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                        row["abs_return_1d"],
                        row["abs_return_5d"],
                        row["vol_vs_avg_20"],
                    ),
                )
                written += cur.rowcount
            updated += 1

        latest = max((b.date for bars in bars_map.values() for b in bars), default=None)
        no_data = sorted(set(no_data))
        note = (
            f"ticker aggiornati={updated}, senza dati={len(no_data)}; "
            f"fonte primaria=yfinance, fallback=stooq; period_days={period_days}"
        )
        return ModuleResult(
            module_key=self.key,
            status="ok",
            rows_written=written,
            errors=[],
            watermark=latest,
            note=note,
        )