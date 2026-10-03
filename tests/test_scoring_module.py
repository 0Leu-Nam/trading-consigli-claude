"""Test del modulo di scoring (Fase 6).

Le regressioni piu' importanti non sono sul "quanto" ma sul "quando non
contribuisce": un modulo assente deve restare assente, non trasformarsi in uno
zero che penalizza il ticker. E' il caso normale sui dati reali (nessuna
company ha tutti e 4 i segnali, 122 ne hanno 2, 15 ne hanno 3).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import db
from core.module_interface import RunContext
from core.orchestrator import classify_run
from modules.scoring import sources
from modules.scoring.module import Module

WEIGHTS = {
    "insider_open_market_buy": 30,
    "insider_open_market_sell": -20,
    "price_volume_spike": 15,
    "price_move_up": 10,
    "news_sentiment_positive": 20,
    "news_sentiment_negative": -15,
    "institutional_new_position": 25,
    "institutional_increase": 15,
    "institutional_multiple": 5,
}
WINDOWS = {
    "insider": {"days": 7, "min_value_usd": 50_000, "open_market_only": True},
    "price": {"days": 5, "vol_spike_mult": 3.0, "move_5d_pct": 0.10},
    "news": {"days": 14, "positive_gte": 0.35, "negative_lte": -0.35, "min_articles": 3},
    "institutional": {"quarters": 1, "max_age_days": 120},
}
DEFAULT_CONFIG = {
    "enabled": False,
    "recalc": False,
    "min_signals": 2,
    "shortlist_size": 25,
    "windows": WINDOWS,
    "weights": WEIGHTS,
}
TODAY = date.today()
LAST_BAR = (TODAY - timedelta(days=2)).isoformat()


def _quarter(offset: int) -> str:
    """Trimestre chiusi con il normale ritardo di deposito dei 13F: con
    offset=1 si ottiene l'ultimo trimestre depositabile (l'ultimo chiuso non
    e' ancora uscito entro 45gg dalla fine)."""
    index = ((TODAY.year * 4 + (TODAY.month - 1) // 3) - offset)
    return f"{index // 4}Q{index % 4 + 1}"


LATEST_QUARTER = _quarter(1)
PREV_QUARTER = _quarter(2)


# ── fixture ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def ctx_and_conn(tmp_path):
    """DB pulito con UNA sola barra di prezzo su un ticker di base.

    La barra serve a dare al modulo una `signal_date` (che prende da
    price_snapshots, non da date.today()); non e' un segnale perche' non ha
    indicatori. Ogni test che vuole il contributo prezzo chiama add_price sul
    proprio ticker.
    """
    path = tmp_path / "app.db"
    db.init_schema(path)
    conn = db.connect(path)
    base = db.upsert_company(conn, "BASE", name="BASE CO")
    conn.execute(
        """INSERT INTO price_snapshots (company_id, symbol, date, close, volume)
           VALUES (?, 'BASE', ?, 1.0, 1)""",
        (base, LAST_BAR),
    )
    conn.commit()
    ctx = RunContext(
        conn=conn,
        module_config=_copy_config(DEFAULT_CONFIG),
        global_config={},
        env_getter=lambda k, d=None: d,
    )
    yield ctx, conn, path
    conn.close()


def _copy_config(d):
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in d.items()}


def add_company(conn, ticker, name=None):
    return db.upsert_company(conn, ticker, name=name or ticker)


def add_price(conn, company_id, *, date_=None, vol_vs_avg_20=None, abs_return_5d=None):
    conn.execute(
        """INSERT OR IGNORE INTO price_snapshots
             (company_id, symbol, date, open, high, low, close, volume,
              abs_return_1d, abs_return_5d, vol_vs_avg_20)
           VALUES (?, (SELECT ticker FROM companies WHERE id = ?), ?, 1, 1, 1, 1, 1, NULL, ?, ?)
        """,
        (company_id, company_id, date_ or LAST_BAR, abs_return_5d, vol_vs_avg_20),
    )
    conn.commit()


def add_insider(conn, company_id, *, kind="P", value=1_000_000, days_ago=1, open_market=1):
    """Il filing e' identificato da (accession, row_no): perche' una stessa
    azienda possa avere acquisto E vendita, l'accession include il tipo di
    transazione, altrimenti il secondo INSERT OR IGNORE verrebbe scartato in
    silenzio e il test passerebbe senza il contributo che crede di avere."""
    conn.execute(
        """INSERT OR IGNORE INTO insider_transactions
             (company_id, accession, row_no, filing_date, transaction_date,
              insider_name, transaction_type, shares, price_per_share,
              value_usd, is_open_market)
           VALUES (?, ?, 1, ?, ?, 'TEST INSIDER', ?, 1000, 10, ?, ?)
        """,
        (
            company_id,
            f"{company_id:010d}-{kind}-26-{days_ago:06d}",
            (TODAY - timedelta(days=days_ago)).isoformat(),
            (TODAY - timedelta(days=days_ago)).isoformat(),
            kind,
            value,
            open_market,
        ),
    )
    conn.commit()


def add_news(conn, company_id, scores, *, days_ago=1):
    for i, score in enumerate(scores):
        conn.execute(
            """INSERT OR IGNORE INTO news_events
                 (company_id, uuid, published_at, source, title, sentiment_score, sentiment_label)
               VALUES (?, ?, ?, 'test', 't', ?, 'x')
            """,
            (
                company_id,
                f"{company_id:010d}-news-{i:03d}",
                f"{(TODAY - timedelta(days=days_ago)).isoformat()}T12:00:00+00:00",
                score,
            ),
        )
    conn.commit()


def add_holding(conn, company_id, *, quarter, filer_cik, cusip, shares, filer_name="TEST FUND"):
    conn.execute(
        """INSERT OR IGNORE INTO institutional_holdings
             (company_id, filing_quarter, filing_date, filer_name, filer_cik,
              issuer_name, cusip, shares, value_usd)
           VALUES (?, ?, ?, ?, ?, 'ISSUER', ?, ?, 1000)
        """,
        (company_id, quarter, f"{quarter[:4]}-08-14", filer_name, filer_cik, cusip, shares),
    )
    conn.commit()


def add_ignore(conn, company_id, note="no"):
    conn.execute(
        "INSERT OR REPLACE INTO watchlist (company_id, note, status) VALUES (?, ?, 'ignore')",
        (company_id, note),
    )
    conn.commit()


def composite(conn, ticker):
    row = conn.execute(
        """SELECT s.magnitude, s.description FROM signals s
           JOIN companies co ON co.id = s.company_id
           WHERE co.ticker = ? AND s.module_key = 'scoring' AND s.signal_type = 'composite'""",
        (ticker,),
    ).fetchone()
    return row


def contribution_rows(conn, ticker):
    return conn.execute(
        """SELECT s.module_key, s.signal_type, s.magnitude, s.direction, s.description
           FROM signals s JOIN companies co ON co.id = s.company_id
           WHERE co.ticker = ? AND s.signal_type != 'composite' ORDER BY s.signal_type""",
        (ticker,),
    ).fetchall()


# ── 1. pesi in config, non nel codice ─────────────────────────────────────────


def test_weights_come_from_config(ctx_and_conn):
    """Il peso non e' nel codice: cambiarlo in config cambia lo score."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)
    add_news(conn, cid, [0.8, 0.8, 0.8])

    Module().run(ctx)
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(50.0)  # 30 + 20

    # stesso input, pesi diversi -> score diverso
    conn.execute("DELETE FROM signals")
    ctx.module_config["weights"] = {**WEIGHTS, "insider_open_market_buy": 5}
    Module().run(ctx)
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(25.0)  # 5 + 20


def test_missing_weights_is_a_visible_error(ctx_and_conn):
    ctx, _conn, _path = ctx_and_conn
    ctx.module_config["weights"] = {}
    result = Module().run(ctx)
    assert result.status == "error"
    assert "weights" in result.errors[0]


def test_no_price_bars_is_an_error_not_an_empty_success(tmp_path):
    """Senza prezzo non c'e' nemmeno la data del segnale: fallire in silenzio
    produrrebbe uno 'ok' a zero righe."""
    path = tmp_path / "vuoto.db"
    db.init_schema(path)
    conn = db.connect(path)
    ctx = RunContext(conn=conn, module_config=_copy_config(DEFAULT_CONFIG), global_config={},
                     env_getter=lambda k, d=None: d)
    result = Module().run(ctx)
    assert result.status == "error"
    assert "price_snapshots" in result.errors[0]


# ── 2. dati mancanti: contributo 0, copertura esplicita ───────────────────────


def test_absent_module_neither_contributes_nor_penalises(ctx_and_conn):
    """Il caso normale sui dati reali. AAA ha solo insider buying (+30); non
    deve diventare +30-20-15 = -5 perche' news e prezzo non ci sono."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)

    Module().run(ctx)
    row = composite(conn, "AAA")
    assert row["magnitude"] == pytest.approx(30.0)
    assert "copertura 1/4" in row["description"]
    assert "copertura 1/4 (insider_trading)" in row["description"]


def test_coverage_counts_only_modules_that_actually_spoke(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)
    add_price(conn, cid, vol_vs_avg_20=5.0)

    Module().run(ctx)
    row = composite(conn, "AAA")
    assert "copertura 2/4 (insider_trading,price_screener)" in row["description"]
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(45.0)  # 30 + 15


def test_two_contributions_from_same_module_count_as_one_module(ctx_and_conn):
    """Copertura = moduli distinti, non numero di segnali: 3 acquisti insider
    non valgono come 3 moduli diversi."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid, kind="P", days_ago=1)
    add_insider(conn, cid, kind="P", days_ago=2)

    Module().run(ctx)
    rows = contribution_rows(conn, "AAA")
    assert len(rows) == 1  # la query aggrega per company_id e transaction_type
    assert "copertura 1/4" in composite(conn, "AAA")["description"]


def test_institutional_signal_still_counts_when_price_is_missing(ctx_and_conn):
    """Il 13F copre solo 112 company (i 3 gestori), molte fuori dall'universo
    di prezzo: il contributo deve arrivare lo stesso."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "MU")
    add_price(conn, cid)  # solo per dare una signal_date
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="111", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="111", shares=200)

    Module().run(ctx)
    rows = contribution_rows(conn, "MU")
    assert [r["signal_type"] for r in rows] == ["institutional_increase"]
    assert "copertura 1/4" in composite(conn, "MU")["description"]


# ── 3. trasparenza e tracciabilita' ───────────────────────────────────────────


def test_each_contribution_has_its_own_row_with_its_weight(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)
    add_price(conn, cid, vol_vs_avg_20=4.0)
    Module().run(ctx)

    rows = contribution_rows(conn, "AAA")
    assert [r["signal_type"] for r in rows] == ["insider_open_market_buy", "price_volume_spike"]
    assert [r["magnitude"] for r in rows] == [30.0, 15.0]
    # ogni contributo dice perche', in chiaro
    assert "acquisto open-market" in rows[0]["description"]
    assert "4.0x" in rows[1]["description"]
    # e la sintesi riporta il totale e la copertura
    row = composite(conn, "AAA")
    assert row["magnitude"] == pytest.approx(45.0)
    assert "2 contributi" in row["description"]


def test_negative_contributions_have_direction_minus_one(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_insider(conn, cid, kind="S")
    add_news(conn, cid, [-0.9, -0.9, -0.9])

    Module().run(ctx)
    by_type = {r["signal_type"]: r for r in contribution_rows(conn, "AAA")}
    assert by_type["insider_open_market_sell"]["direction"] == -1
    assert by_type["news_sentiment_negative"]["direction"] == -1
    assert by_type["insider_open_market_sell"]["magnitude"] == -20.0
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(-35.0)


# ── 4. determinismo e idempotenza ────────────────────────────────────────────


def test_two_identical_runs_do_not_duplicate_or_change(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)
    add_news(conn, cid, [0.9, 0.9, 0.9])

    Module().run(ctx)
    conn.commit()
    first = conn.execute(
        "SELECT COUNT(*) n, SUM(magnitude) s FROM signals WHERE signal_type != 'composite'"
    ).fetchone()
    before = composite(conn, "AAA")["magnitude"]

    second_result = Module().run(ctx)
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) n, SUM(magnitude) s FROM signals WHERE signal_type != 'composite'"
    ).fetchone()

    assert (after["n"], after["s"]) == (first["n"], first["s"])
    assert composite(conn, "AAA")["magnitude"] == before
    assert second_result.watermark == LAST_BAR


def test_recalc_flag_overwrites_same_key(ctx_and_conn):
    """Con recalc: true la sovrascrittura è esplicita: il peso cambiato si
    vede senza aspettare un nuovo giorno."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)
    add_price(conn, cid, vol_vs_avg_20=5.0)

    Module().run(ctx)
    conn.commit()
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(45.0)   # 30 + 15

    ctx.module_config["recalc"] = True
    ctx.module_config["weights"] = {**WEIGHTS, "insider_open_market_buy": 1}
    Module().run(ctx)
    conn.commit()
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(16.0)   # 1 + 15
    # e non duplica: la chiave UNIQUE tiene
    n = conn.execute(
        "SELECT COUNT(*) n FROM signals WHERE signal_type = 'composite'"
    ).fetchone()["n"]
    assert n == 1


def test_signal_date_is_last_price_bar_not_today(ctx_and_conn):
    """Se signal_date fosse date.today(), un ricalcolo il giorno dopo sullo
    stesso dataset creerebbe righe nuove invece di essere idempotente."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)
    add_price(conn, cid, date_="2026-01-15")
    conn.execute("UPDATE price_snapshots SET date = '2026-01-15' WHERE symbol = 'BASE'")
    conn.commit()
    result = Module().run(ctx)
    assert result.watermark == "2026-01-15"
    rows = conn.execute("SELECT DISTINCT signal_date FROM signals").fetchall()
    assert [r["signal_date"] for r in rows] == ["2026-01-15"]


# ── 5. nessun fallimento silenzioso ───────────────────────────────────────────


def test_broken_source_is_isolated_and_visible(ctx_and_conn, monkeypatch):
    """Un extractor che solleva non ferma gli altri: gli altri moduli
    contribuiscono, l'errore resta in errors[] e classifica il run warning."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid)
    add_news(conn, cid, [0.9, 0.9, 0.9])

    def boom(*a, **k):
        raise RuntimeError("tabella news corrotta")

    monkeypatch.setattr(sources, "news_signals", boom)
    result = Module().run(ctx)
    conn.commit()

    assert result.status == "ok"          # non è un fallimento tecnico del run
    assert any("news_sentiment" in e and "corrotta" in e for e in result.errors)
    assert classify_run([result], {"run": {"partial_error_threshold": 0}}) == "warning"
    # insider ha comunque contribuito
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(30.0)
    assert "copertura 1/4" in composite(conn, "AAA")["description"]


def test_no_evaluable_ticker_is_reported(ctx_and_conn):
    """Zero ticker valutabile non puo' essere uno 'ok' a zero righe."""
    ctx, _conn, _path = ctx_and_conn
    result = Module().run(ctx)
    assert result.errors, "nessun ticker valutabile: deve essere visibile"
    assert classify_run([result], {"run": {"partial_error_threshold": 0}}) == "warning"


# ── 6. watchlist ──────────────────────────────────────────────────────────────


def test_ignored_ticker_gets_no_score_even_when_top(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "SKIPME")
    add_insider(conn, cid, value=900_000_000)
    add_price(conn, cid, vol_vs_avg_20=9.0)
    add_news(conn, cid, [1.0, 1.0, 1.0])
    add_ignore(conn, cid)

    result = Module().run(ctx)
    conn.commit()

    assert composite(conn, "SKIPME") is None, "ticker ignorato ha ricevuto un punteggio"
    n = conn.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"]
    assert n == 0
    assert "ignorati da watchlist=1" in result.note
    # nessun ticker e' rimasto valutabile: deve essere visibile, non un 'ok' muto
    assert result.errors
    assert classify_run([result], {"run": {"partial_error_threshold": 0}}) == "warning"


def test_watch_status_is_not_ignored(ctx_and_conn):
    """status='watch' e' il default: non deve escludere dalla shortlist."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "KEPT")
    add_insider(conn, cid)
    add_news(conn, cid, [0.9, 0.9, 0.9])
    conn.execute(
        "INSERT INTO watchlist (company_id, note, status) VALUES (?, ?, 'watch')",
        (cid, "da tenere d'occhio"),
    )
    conn.commit()

    Module().run(ctx)
    assert composite(conn, "KEPT") is not None


# ── 7. min_signals ────────────────────────────────────────────────────────────


def test_min_signals_excludes_single_source_from_shortlist_but_keeps_row(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    solo = add_company(conn, "SOLO")
    add_insider(conn, solo)
    doppio = add_company(conn, "DOPPIO")
    add_insider(conn, doppio)
    add_news(conn, doppio, [0.9, 0.9, 0.9])

    Module().run(ctx)
    # entrambi hanno la riga: la shortlist e' un filtro di lettura, non di scrittura
    assert composite(conn, "SOLO") is not None
    assert composite(conn, "DOPPIO") is not None
    assert "copertura 1/4" in composite(conn, "SOLO")["description"]
    assert "copertura 2/4" in composite(conn, "DOPPIO")["description"]


def test_shortlist_order_is_by_score_then_coverage_then_ticker(ctx_and_conn):
    """A parita' di score vince chi ha piu' copertura, poi il ticker in ordine
    alfabetico: senza i tie-breaker due run identici darebbero ordini diversi."""
    ctx, conn, _path = ctx_and_conn
    for ticker in ("BBB", "AAA"):
        cid = add_company(conn, ticker)
        add_insider(conn, cid)
        add_news(conn, cid, [0.9, 0.9, 0.9])   # 30 + 20 = 50 su entrambi
    Module().run(ctx)
    note = Module().run(ctx).note
    assert "AAA=+50" in note and "BBB=+50" in note
    assert note.index("AAA=+50") < note.index("BBB=+50")


def test_shortlist_size_caps_the_ranking(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    for i in range(5):
        cid = add_company(conn, f"T{i}")
        add_insider(conn, cid, value=100_000 * (i + 1))
        add_news(conn, cid, [0.9, 0.9, 0.9])
    ctx.module_config["shortlist_size"] = 2
    result = Module().run(ctx)
    assert "in shortlist=2" in result.note


# ── 8. regole dei singoli moduli ──────────────────────────────────────────────


def test_insider_requires_open_market(ctx_and_conn):
    """Un P non open-market non e' convinzione: non deve contribuire."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid, kind="P", open_market=0)
    add_news(conn, cid, [0.9, 0.9, 0.9])

    Module().run(ctx)
    types = [r["signal_type"] for r in contribution_rows(conn, "AAA")]
    assert "insider_open_market_buy" not in types


def test_insider_respects_min_value_threshold(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid, value=10_000)          # sotto i 50k
    add_news(conn, cid, [0.9, 0.9, 0.9])
    Module().run(ctx)
    assert "insider_open_market_buy" not in [r["signal_type"] for r in contribution_rows(conn, "AAA")]


def test_insider_ignores_unmapped_transaction_types(ctx_and_conn):
    """M, F, A, C, D, G sono esercizi/assegnazioni: non mappati -> nessun
    contributo e nessun errore."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_insider(conn, cid, kind="M", value=5_000_000)
    add_news(conn, cid, [0.9, 0.9, 0.9])
    result = Module().run(ctx)
    assert result.errors == []
    assert "insider_open_market_buy" not in [r["signal_type"] for r in contribution_rows(conn, "AAA")]


def test_price_move_respects_sign(ctx_and_conn):
    """`abs_return_5d` contiene il RENDIMENTO con segno (misurato: min -0.85,
    max +1.74). Un ribasso del 30% ha lo stesso valore assoluto di un rialzo
    del 30% e significato opposto: non deve generare contributo positivo."""
    ctx, conn, _path = ctx_and_conn
    giu = add_company(conn, "GIU")
    su = add_company(conn, "SU")
    add_price(conn, giu, abs_return_5d=-0.30)
    add_price(conn, su, abs_return_5d=+0.30)
    add_insider(conn, giu)
    add_insider(conn, su)
    add_news(conn, giu, [0.9, 0.9, 0.9])
    add_news(conn, su, [0.9, 0.9, 0.9])

    Module().run(ctx)
    assert "price_move_up" in [r["signal_type"] for r in contribution_rows(conn, "SU")]
    assert "price_move_up" not in [r["signal_type"] for r in contribution_rows(conn, "GIU")]


def test_price_move_joins_on_company_id_not_symbol(ctx_and_conn):
    """Le societa' emerse solo dal Form 4 non sono nell'universo di prezzo e non
    hanno `symbol` corrispondente: il join per symbol perderebbe il contributo
    silenziosamente."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "KOD")
    add_insider(conn, cid)
    add_news(conn, cid, [0.9, 0.9, 0.9])
    # nessuna price_snapshots per KOD: il run deve funzionare lo stesso
    Module().run(ctx)
    assert composite(conn, "KOD")["magnitude"] == pytest.approx(50.0)


def test_news_needs_min_articles(ctx_and_conn):
    """Su dati reali 711 articoli su 1082 sono neutral: la media di un solo
    titolo (+1.0) e' piu' estrema di una diffusa (+0.2) e va contenuta."""
    ctx, conn, _path = ctx_and_conn
    uno = add_company(conn, "UNO")
    molti = add_company(conn, "MOLTI")
    add_price(conn, uno)
    add_price(conn, molti)
    add_news(conn, uno, [1.0])                 # 1 articolo solo
    add_news(conn, molti, [0.4, 0.4, 0.4])    # 3 articoli
    add_insider(conn, uno)
    add_insider(conn, molti)

    Module().run(ctx)
    assert "news_sentiment_positive" not in [r["signal_type"] for r in contribution_rows(conn, "UNO")]
    assert "news_sentiment_positive" in [r["signal_type"] for r in contribution_rows(conn, "MOLTI")]


def test_institutional_new_position_and_increase(ctx_and_conn):
    """Il calcolo e' qui, non letto da shares_delta (che e' NULL su tutto il DB:
    il modulo 13F confronta col trimestre precedente dentro lo stesso run e con
    quarters_back: 2 il primo trimestre non trova un precedente)."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="BBB", shares=5)

    Module().run(ctx)
    types = sorted(r["signal_type"] for r in contribution_rows(conn, "AAA"))
    assert types == ["institutional_new_position"]
    # il delta calcolato qui, non letto dalla colonna NULL
    rows = contribution_rows(conn, "AAA")
    assert "nuova posizione" in rows[0]["description"]


def test_institutional_increase_detected(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=250)

    Module().run(ctx)
    rows = contribution_rows(conn, "AAA")
    assert [r["signal_type"] for r in rows] == ["institutional_increase"]
    assert "100->250" in rows[0]["description"]


def test_institutional_multiple_filers_bonus(ctx_and_conn):
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    for cik in ("0000000001", "0000000002"):
        add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik=cik, cusip="AAA", shares=100)
        add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik=cik, cusip="AAA", shares=200)

    Module().run(ctx)
    rows = contribution_rows(conn, "AAA")
    types = sorted(r["signal_type"] for r in rows)
    assert "institutional_multiple" in types

    # i due aumenti hanno la stessa (company, module_key, signal_type, data) e
    # quindi la stessa chiave UNIQUE: vengono aggregati in una riga da 30 con
    # entrambe le descrizioni, non scartati in silenzio da INSERT OR IGNORE
    increase = next(r for r in rows if r["signal_type"] == "institutional_increase")
    assert increase["magnitude"] == pytest.approx(30.0)   # 15 + 15
    assert increase["description"].count("TEST FUND") == 2
    # e il bonus di convergenza e' un contributo a se': 30 + 5 = 35
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(35.0)
    bonus = next(r for r in rows if r["signal_type"] == "institutional_multiple")
    assert "2 gestori" in bonus["description"]


def test_institutional_ignores_exit_position(ctx_and_conn):
    """Una posizione che sparisce non e' un segnale: e' rumore."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    # 'AAA' era aperta nel trimestre precedente e non c'e' piu' nel corrente:
    # il gestore ha chiuso. Una posizione stabile ('KEEP') resta com'era, e nel
    # corrente non deve comparire nessun CUSIP nuovo (altrimenti sarebbe una
    # nuova posizione, non un'uscita).
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="KEEP", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="KEEP", shares=100)

    result = Module().run(ctx)
    assert contribution_rows(conn, "AAA") == []
    # l'unico errore possibile riguarda BASE (prezzo senza segnali), non il 13F
    assert all("institutional" not in e for e in result.errors), result.errors


def test_stale_institutional_is_not_an_error(ctx_and_conn):
    """Un 13F vecchio oltre max_age_days non e' un errore e non contribuisce:
    e' il segnale che semplicemente non c'e' piu'. Se fosse trattato come
    errore, ogni run dopo 120gg segnalerebbe un problema che non esiste."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=200)

    ctx.module_config["windows"] = {**WINDOWS, "institutional": {"quarters": 1, "max_age_days": 1}}
    result = Module().run(ctx)
    conn.commit()
    assert contribution_rows(conn, "AAA") == []
    # l'unico errore e' che BASE non ha segnali: NON c'e' un errore sul 13F
    assert all("institutional" not in e for e in result.errors), result.errors


def test_institutional_needs_two_quarters(ctx_and_conn):
    """Con un solo trimestre non si puo' parlare di 'aumento': nessun
    contributo e nessun errore."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)

    result = Module().run(ctx)
    assert contribution_rows(conn, "AAA") == []
    assert all("institutional" not in e for e in result.errors), result.errors


def test_institutional_description_states_the_age(ctx_and_conn):
    """L'eta' del 13F finisce nella description: 45-90gg di lag sono la norma,
    ma in dashboard deve essere visibile che quel segnale e' vecchio."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=200)

    Module().run(ctx)
    rows = contribution_rows(conn, "AAA")
    assert LATEST_QUARTER in rows[0]["description"]
    assert "gg fa" in rows[0]["description"]


# ── segnali contrastanti ────────────────────────────────────────────────────────


def test_opposite_contributions_are_flagged_without_changing_the_score(ctx_and_conn):
    """Caso reale CBRS (2026-10-02, DB di produzione): 36 insider in vendita
    open-market per 87.7M (-20) e una nuova posizione di Altimeter (+50) con
    bonus multi-gestore (+5) finivano in un +35 che sembrava pieno.

    Il peso resta -20 e il punteggio resta 35: la vendita non viene bloccata,
    resta un contributo negativo. Quello che cambia e' che la riga di sintesi
    ADMETTE che i contributi vanno in direzioni opposte.
    """
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "CBRS")
    add_price(conn, cid)
    add_insider(conn, cid, kind="S", value=87_700_000, days_ago=2)
    # due trimestri servono perche' il segnale istituzionale confronta
    # l'ultimo con il precedente: nel trimestre precedente CBRS non c'era
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="OTHER", shares=10)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="CBRS", shares=10)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000002", cusip="CBRS", shares=20)

    result = Module().run(ctx)

    assert composite(conn, "CBRS")["magnitude"] == pytest.approx(35.0)
    types = sorted(r["signal_type"] for r in contribution_rows(conn, "CBRS"))
    assert types == ["insider_open_market_sell", "institutional_multiple", "institutional_new_position"]

    desc = composite(conn, "CBRS")["description"]
    assert "segnali contrastanti" in desc
    assert "insider_open_market_sell" in desc and "institutional_new_position" in desc
    # la copertura resta dichiarata accanto al contrasto: il conflitto spiega
    # il totale, non sostituisce l'informazione su quali moduli hanno parlato
    assert "copertura 2/4" in desc

    # e la shortlist lo rende visibile senza aprire ogni riga
    assert "segnali contrastanti=1" in result.note
    assert "CBRS" in result.note


def test_agreement_is_not_labelled_as_conflict(ctx_and_conn):
    """Il caso normale non deve accumulare etichette: due fonti che concordano
    producono una description senza 'contrastanti'."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_insider(conn, cid, kind="P", value=27_500_000, days_ago=2)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=10)

    result = Module().run(ctx)

    assert "contrastanti" not in composite(conn, "AAA")["description"]
    assert "segnali contrastanti" not in result.note


def test_small_negative_against_large_positive_is_not_a_conflict(ctx_and_conn):
    """Un contributo sotto soglia non e' un disaccordo fra fonti: e' rumore.
    +50 istituzionale e -5 di altro peso non sono un conflitto, altrimenti
    l'etichetta segnalerebbe quasi tutti i ticker con piu' di due moduli."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="OTHER", shares=10)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=10)
    ctx.module_config["weights"] = {
        **ctx.module_config["weights"],
        # un peso negativo piccolo: reale segnale, ma non un disaccordo
        "news_sentiment_negative": -5,
    }
    add_news(conn, cid, [-0.9] * 5, days_ago=2)

    result = Module().run(ctx)

    assert composite(conn, "AAA")["magnitude"] == pytest.approx(20.0)   # +25 - 5
    assert "contrastanti" not in composite(conn, "AAA")["description"]
    assert "segnali contrastanti" not in result.note


def test_conflict_threshold_is_configurable(ctx_and_conn):
    """La soglia viene dalla config, non dal codice: chi cambia regime deve
    cambiare la segnaletica senza toccare il modulo."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_insider(conn, cid, kind="S", value=1_000_000, days_ago=2)
    add_insider(conn, cid, kind="P", value=1_000_000, days_ago=2)
    ctx.module_config["conflict_min_weight"] = 25

    Module().run(ctx)

    # +30 e -20 non superano 25: nessun conflitto dichiarato, ma i due
    # contributi ci sono entrambi e il punteggio e' la loro somma
    types = sorted(r["signal_type"] for r in contribution_rows(conn, "AAA"))
    assert types == ["insider_open_market_buy", "insider_open_market_sell"]
    assert composite(conn, "AAA")["magnitude"] == pytest.approx(10.0)
    assert "contrastanti" not in composite(conn, "AAA")["description"]


def test_conflict_does_not_change_shortlist_score_or_order(ctx_and_conn):
    """Il contrasto e' informazione, non filtro ne penalita': un ticker con
    segnali contrastanti resta in shortlist e viene ordinato per score come
    tutti gli altri."""
    ctx, conn, _path = ctx_and_conn
    # CBRS reale: -20 sell + 2x25 nuova posizione + 5 multi = +35, contrasto
    cbrs = add_company(conn, "CBRS")
    add_price(conn, cbrs)
    add_insider(conn, cbrs, kind="S", value=1_000_000, days_ago=2)
    add_holding(conn, cbrs, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="OTHER", shares=1)
    add_holding(conn, cbrs, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="CBRS", shares=1)
    add_holding(conn, cbrs, quarter=LATEST_QUARTER, filer_cik="0000000002", cusip="CBRS", shares=1)
    # ACCO: +50 (+30 insider, +20 news) con due fonti allineate
    acco = add_company(conn, "ACCO")
    add_price(conn, acco)
    add_insider(conn, acco, kind="P", value=1_000_000, days_ago=2)
    add_news(conn, acco, [0.9, 0.9, 0.9], days_ago=2)

    result = Module().run(ctx)

    assert composite(conn, "CBRS")["magnitude"] == pytest.approx(35.0)
    assert composite(conn, "ACCO")["magnitude"] == pytest.approx(50.0)
    assert "contrastanti" in composite(conn, "CBRS")["description"]
    assert "contrastanti" not in composite(conn, "ACCO")["description"]
    # entrambi in shortlist: il contrasto non ha escluso nessuno
    assert "in shortlist=2" in result.note
    # e l'ordine segue il punteggio, non l'etichetta: ACCO (+50) prima di CBRS
    assert result.note.index("ACCO=+50") < result.note.index("CBRS=+35")