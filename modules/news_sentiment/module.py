"""Modulo news_sentiment: notizie + sentiment (screening, nessun ordine).

Raccolta headline per una subset di ticker (priorità: insider recente →
watchlist → resto universe, fino a ``max_symbols``), sentiment ibrido
(Marketaux quando fornisce lo score, altrimenti lessico locale) e inserimento
in ``news_events`` deduped da UNIQUE(uuid).

Isolamento delle fonti: ogni fetch per-ticker è in try/except; una fonte giù o
con formato cambiato N ON blocca le altre né il run. La semantica di status:
- ``ok`` anche con 0 righe "nuove" quando le fonti rispondono correttamente
  (nessuna notizia disponibile, o solo articoli già presenti);
- ``ok`` con degradazione annotata se UNA fonte è fuori uso ma l'altra ha dato dati;
- ``error`` solo con fallimento tecnico di TUTTE le fonti e 0 righe inserite.
"""

import logging
from datetime import date, timedelta

from core import db
from core.module_interface import ModuleInterface, ModuleResult, RunContext
from modules.news_sentiment import data_sources, sentiment

logger = logging.getLogger(__name__)


def select_tickers(conn, universe: list[str], *, max_symbols: int, lookback_insider_days: int) -> list[str]:
    """Ordina i ticker da coprire: insider recenti → watchlist → resto universe."""
    cutoff = (date.today() - timedelta(days=lookback_insider_days)).isoformat()
    ordered: list[str] = []
    seen: set[str] = set()

    def _append(tickers_with_priority) -> None:
        for ticker in tickers_with_priority:
            ticker = str(ticker).strip().upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                ordered.append(ticker)

    insider = [
        r["ticker"]
        for r in conn.execute(
            """
            SELECT DISTINCT c.ticker
            FROM insider_transactions t
            JOIN companies c ON c.id = t.company_id
            WHERE t.filing_date >= ?
            """,
            (cutoff,),
        )
    ]
    _append(insider)

    watchlist = [
        r["ticker"]
        for r in conn.execute(
            "SELECT c.ticker FROM watchlist w JOIN companies c ON c.id = w.company_id"
        )
    ]
    _append(watchlist)

    _append(universe)

    return ordered[:max_symbols]


class Module(ModuleInterface):
    key = "news_sentiment"
    display_name = "Notizie e sentiment"

    def run(self, ctx: RunContext) -> ModuleResult:
        max_symbols = int(ctx.get("max_symbols", 40))
        lookback_days = int(ctx.get("lookback_insider_days", 7))
        sentiment_gte = float(ctx.get("sentiment_gte", 0.35))
        sources = [str(s) for s in ctx.get("sources", ["marketaux", "yahoo_finance_rss"])]
        marketaux_token = ctx.env("MARKETAUX_API_TOKEN") or None

        try:
            from modules.price_screener.module import resolve_universe

            universe = resolve_universe(ctx.module_config)
        except ValueError as exc:
            return ModuleResult(module_key=self.key, status="error", errors=[str(exc)])

        tickers = select_tickers(ctx.conn, universe, max_symbols=max_symbols, lookback_insider_days=lookback_days)
        if not tickers:
            return ModuleResult(module_key=self.key, status="skipped", note="nessun ticker selezionato")

        usable = [s for s in sources if s != "marketaux" or marketaux_token]
        if not usable:
            return ModuleResult(module_key=self.key, status="error", errors=["nessuna fonte utilizzabile (serve MARKETAUX_API_TOKEN o yahoo_finance_rss)"])

        source_ok = {s: 0 for s in usable}
        source_fail = {s: 0 for s in usable}
        items_by_symbol: dict[str, list[data_sources.NewsItem]] = {}

        for ticker in tickers:
            ticker_items: list[data_sources.NewsItem] = []
            for source in usable:
                try:
                    if source == "marketaux":
                        items = data_sources.fetch_marketaux(ticker, marketaux_token)
                        for it in items:
                            if it.sentiment_score is None:
                                it.sentiment_score = sentiment.sentiment_score(it.title)
                    else:
                        items = data_sources.fetch_yahoo_rss(ticker)
                        for it in items:
                            it.sentiment_score = sentiment.sentiment_score(it.title)
                    source_ok[source] += 1
                    ticker_items.extend(items)
                except data_sources.NewsSourceError as exc:
                    source_fail[source] += 1
                    logger.warning("%s: %s", source, exc)
            if ticker_items:
                items_by_symbol[ticker] = ticker_items

        written = 0
        fetched = 0
        for symbol, items in items_by_symbol.items():
            company_id = db.upsert_company(ctx.conn, symbol)
            fetched += len(items)
            for it in items:
                label = sentiment.sentiment_label(it.sentiment_score, sentiment_gte)
                cur = ctx.conn.execute(
                    """
                    INSERT OR IGNORE INTO news_events
                        (company_id, uuid, published_at, source, title, url,
                         sentiment_score, sentiment_label)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (company_id, it.uuid, it.published_at, it.source, it.title, it.url,
                     it.sentiment_score, label),
                )
                written += cur.rowcount

        latest = max(
            (it.published_at for items in items_by_symbol.values() for it in items),
            default=None,
        )
        fail_summary = ", ".join(f"{s}={source_fail[s]}" for s in usable if source_fail[s])
        parts = [
            f"fonti=ok({', '.join(f'{s}:{source_ok[s]}' for s in usable if source_ok[s])})",
        ]
        if fail_summary:
            parts.append(f"fonti=ko({fail_summary})")
        if written == 0 and sum(source_fail.values()) == 0:
            parts.append("nessuna notizia nuova (fonti ok)")
        note = f"ticker coperti={len(items_by_symbol)}, articoli={fetched}, nuovi={written}; " + "; ".join(parts)

        all_sources_failed = all(source_fail[s] == len(tickers) for s in usable)
        if all_sources_failed and written == 0:
            return ModuleResult(
                module_key=self.key,
                status="error",
                rows_written=0,
                errors=[f"tutte le fonti non disponibili: {fail_summary}"],
                note=note,
            )

        return ModuleResult(
            module_key=self.key,
            status="ok",
            rows_written=written,
            errors=[],
            watermark=latest,
            note=note,
        )