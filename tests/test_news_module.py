"""Test end-to-end del modulo news_sentiment su DB temporaneo (rete mockata).

Coprono in modo esplicito l'isolamento tra fonti richiesto in Fase 4:
un fallimento/cambiamento di formato di una fonte non deve bloccare le altre
né l'intero run, e la semantica di status distingue i casi.
"""

from core import db
from core.module_interface import RunContext
from modules.news_sentiment import data_sources, sentiment
from modules.news_sentiment import module as news_mod
from modules.news_sentiment.data_sources import NewsItem, NewsSourceError


def _item(symbol, title, url, published="2026-09-24T10:00:00+00:00", source="yahoo_finance_rss", score=None):
    return NewsItem(
        uuid=data_sources.synthesize_uuid(url, title),
        symbol=symbol,
        published_at=published,
        source=source,
        title=title,
        url=url,
        sentiment_score=score,
    )


def _ctx(tmp_path, *, env=None, module_config=None):
    path = tmp_path / "app.db"
    db.init_schema(path)
    conn = db.connect(path)
    cfg = {
        "max_symbols": 40,
        "lookback_insider_days": 7,
        "sentiment_gte": 0.35,
        "sources": ["marketaux", "yahoo_finance_rss"],
        "universe": ["AAA", "BBB", "CCC"],
    }
    cfg.update(module_config or {})
    env_map = env or {}

    def env_getter(k, d=None):
        return env_map.get(k, d)

    return RunContext(conn=conn, module_config=cfg, global_config={}, env_getter=env_getter), conn, str(path)


# ── selezione prioritaria ─────────────────────────────────────────────────────

def test_select_tickers_priority_insider_then_watchlist_then_universe(tmp_path):
    ctx, conn, _ = _ctx(tmp_path, module_config={"universe": ["AAA", "BBB", "CCC", "DDD"]})
    a = db.upsert_company(conn, "AAA")
    b = db.upsert_company(conn, "BBB")
    c = db.upsert_company(conn, "CCC")
    d = db.upsert_company(conn, "DDD")
    conn.execute("INSERT INTO watchlist (company_id, status) VALUES (?, 'watch')", (b,))
    conn.execute(
        "INSERT INTO insider_transactions (company_id, accession, row_no, filing_date, transaction_type, url) "
        "VALUES (?, 'X1', 0, ?, 'P', NULL)",
        (c, "2026-09-24"),  # recente → priorità massima
    )
    conn.execute(
        "INSERT INTO insider_transactions (company_id, accession, row_no, filing_date, transaction_type, url) "
        "VALUES (?, 'X2', 0, ?, 'P', NULL)",
        (a, "2026-01-01"),  # vecchio → NON conta come "recente"
    )
    conn.commit()

    selected = news_mod.select_tickers(conn, ["AAA", "BBB", "CCC", "DDD"], max_symbols=10, lookback_insider_days=7)
    # ordine: insider recente (CCC) → watchlist (BBB) → resto universo (AAA, DDD)
    assert selected == ["CCC", "BBB", "AAA", "DDD"]

    capped = news_mod.select_tickers(conn, ["AAA", "BBB", "CCC", "DDD"], max_symbols=2, lookback_insider_days=7)
    assert capped == ["CCC", "BBB"]


# ── isolamento fonti ─────────────────────────────────────────────────────────

def test_yahoo_failure_does_not_block_marketaux_or_the_run(tmp_path, monkeypatch):
    ctx, conn, _ = _ctx(tmp_path, env={"MARKETAUX_API_TOKEN": "T"})

    def _yahoo_broken(ticker, *a, **k):
        raise NewsSourceError(f"Yahoo RSS {ticker}: HTTP 404 (cambio formato?)")

    def _marketaux_ok(ticker, token, *a, **k):
        return [_item(ticker, f"{ticker} strong earnings rally", f"https://mk/{ticker}", source="marketaux", score=0.9)]

    monkeypatch.setattr(data_sources, "fetch_yahoo_rss", _yahoo_broken)
    monkeypatch.setattr(data_sources, "fetch_marketaux", _marketaux_ok)

    r = news_mod.Module().run(ctx)
    assert r.status == "ok"
    assert r.rows_written == 3
    assert r.errors == []  # degradazione riportata in note, non come errore del run
    assert "fonti=ko(yahoo_finance_rss=3)" in r.note


def test_marketaux_failure_does_not_block_yahoo_or_the_run(tmp_path, monkeypatch):
    ctx, conn, _ = _ctx(tmp_path, env={"MARKETAUX_API_TOKEN": "T"})

    def _marketaux_broken(ticker, token, *a, **k):
        raise NewsSourceError(f"Marketaux {ticker}: HTTP 402 quota")

    def _yahoo_ok(ticker, *a, **k):
        return [_item(ticker, f"{ticker} beats records today", f"https://y/{ticker}")]

    monkeypatch.setattr(data_sources, "fetch_marketaux", _marketaux_broken)
    monkeypatch.setattr(data_sources, "fetch_yahoo_rss", _yahoo_ok)

    r = news_mod.Module().run(ctx)
    assert r.status == "ok"
    assert r.rows_written == 3
    assert r.errors == []
    assert "fonti=ko(marketaux=3)" in r.note


def test_malformed_feed_for_one_ticker_keeps_others(tmp_path, monkeypatch):
    # Formato cambiato per UN solo ticker: gli altri devono essere inseriti.
    ctx, conn, _ = _ctx(tmp_path)

    def _yahoo_mixed(ticker, *a, **k):
        if ticker == "BBB":
            raise NewsSourceError(f"Yahoo RSS {ticker}: XML non valido")
        return [_item(ticker, f"{ticker} news", f"https://y/{ticker}")]

    monkeypatch.setattr(data_sources, "fetch_yahoo_rss", _yahoo_mixed)

    r = news_mod.Module().run(ctx)
    assert r.status == "ok"
    assert r.rows_written == 2  # AAA e CCC
    assert r.errors == []
    assert "fonti=ko(yahoo_finance_rss=1)" in r.note


def test_all_sources_technical_failure_is_error(tmp_path, monkeypatch):
    ctx, conn, _ = _ctx(tmp_path, env={"MARKETAUX_API_TOKEN": "T"})

    monkeypatch.setattr(
        data_sources,
        "fetch_yahoo_rss",
        lambda t, *a, **k: (_ for _ in ()).throw(NewsSourceError(f"Yahoo {t}: giù")),
    )
    monkeypatch.setattr(
        data_sources,
        "fetch_marketaux",
        lambda t, token, *a, **k: (_ for _ in ()).throw(NewsSourceError(f"Marketaux {t}: quota esaurita")),
    )

    r = news_mod.Module().run(ctx)
    assert r.status == "error"
    assert r.rows_written == 0
    assert r.errors  # il fallimento tecnico reale è segnalato come errore


# ── nessuna notizia nuova ≠ fallimento tecico ────────────────────────────────

def test_no_news_when_sources_ok_is_not_error(tmp_path, monkeypatch):
    ctx, conn, _ = _ctx(tmp_path)

    monkeypatch.setattr(data_sources, "fetch_yahoo_rss", lambda t, *a, **k: [])  # fonti ok, zero articoli

    r = news_mod.Module().run(ctx)
    assert r.status == "ok"
    assert r.rows_written == 0
    assert r.errors == []
    assert "nessuna notizia nuova (fonti ok)" in r.note


def test_articles_already_present_second_run_ok(tmp_path, monkeypatch):
    ctx, conn, _ = _ctx(tmp_path)
    items = {"AAA": [_item("AAA", "first headline", "https://y/aaa")],
             "BBB": [_item("BBB", "second headline", "https://y/bbb")],
             "CCC": [_item("CCC", "third headline", "https://y/ccc")]}
    monkeypatch.setattr(data_sources, "fetch_yahoo_rss", lambda t, *a, **k: items.get(t, []))

    r1 = news_mod.Module().run(ctx)
    conn.commit()
    assert r1.status == "ok"
    assert r1.rows_written == 3

    r2 = news_mod.Module().run(ctx)
    conn.commit()
    assert r2.status == "ok"
    assert r2.rows_written == 0  # uuid già inseriti → dedup idempotente

    with db.connect(tmp_path / "app.db") as c:
        assert c.execute("SELECT COUNT(*) AS n FROM news_events").fetchone()["n"] == 3
    conn.close()


# ── inserimento e sentiment ibrido ───────────────────────────────────────────

def test_marketaux_sentiment_wins_over_lexicon(tmp_path, monkeypatch):
    ctx, conn, _ = _ctx(tmp_path, env={"MARKETAUX_API_TOKEN": "T"})

    def _marketaux(ticker, token, *a, **k):
        return [_item(ticker, "rallies on record growth", f"https://mk/{ticker}", source="marketaux", score=0.9)]

    def _yahoo(ticker, *a, **k):
        return [_item(ticker, "plain neutral sentence", f"https://y/{ticker}")]

    monkeypatch.setattr(data_sources, "fetch_marketaux", _marketaux)
    monkeypatch.setattr(data_sources, "fetch_yahoo_rss", _yahoo)

    r = news_mod.Module().run(ctx)
    conn.commit()
    assert r.rows_written == 6  # 3 ticker × 2 fonti

    with db.connect(tmp_path / "app.db") as c:
        mk = c.execute("SELECT title, sentiment_score, sentiment_label FROM news_events WHERE source='marketaux'").fetchall()
        yh = c.execute("SELECT title, sentiment_score, sentiment_label FROM news_events WHERE source='yahoo_finance_rss'").fetchall()
        assert all(row["sentiment_score"] == 0.9 and row["sentiment_label"] == "positive" for row in mk)
        assert all(row["sentiment_label"] in ("positive", "negative", "neutral") for row in yh)  # lessico applicato
    conn.close()