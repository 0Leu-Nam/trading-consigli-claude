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
from modules.scoring.module import SINGLE_SOURCE_LABEL, Module

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
    "institutional": {"quarters": 1},
}
# Gli stessi numeri di config.yaml: senza questi la scala insider non avrebbe
# bande e il contributo cadrebbe sul fallback default del modulo.
INSIDER_SCALE = {
    "frac_buy": [{"lt": 0.01, "mult": 0.15},
                 {"gte": 0.50, "mult": 1.6}, {"gte": 0.20, "mult": 1.3},
                 {"gte": 0.05, "mult": 1.0}, {"gte": 0.0, "mult": 0.7}],
    "frac_sell": [{"lt": 0.01, "mult": 0.15},
                   {"gte": 0.50, "mult": 1.6}, {"gte": 0.20, "mult": 1.3},
                   {"gte": 0.05, "mult": 1.0}, {"gte": 0.0, "mult": 0.7}],
    "by_insider_count": [{"gte": 4, "mult": 1.35}, {"gte": 2, "mult": 1.15}, {"gte": 1, "mult": 1.0}],
    "usd_fallback": {
        "buy": [{"gte_usd": 50_000_000, "mult": 1.40}, {"gte_usd": 10_000_000, "mult": 1.13},
                {"gte_usd": 1_000_000, "mult": 0.93}, {"gte_usd": 250_000, "mult": 0.67},
                {"gte_usd": 0, "mult": 0.40}],
        "sell": [{"gte_usd": 50_000_000, "mult": 1.30}, {"gte_usd": 10_000_000, "mult": 1.10},
                 {"gte_usd": 1_000_000, "mult": 0.90}, {"gte_usd": 250_000, "mult": 0.65},
                 {"gte_usd": 0, "mult": 0.35}],
    },
    "max_abs": 60,
}
DEFAULT_CONFIG = {
    # `enabled` e' il segnale che lo Specchio di config.yaml: il modulo non lo
    # legge (l'orchestrator filtra i moduli abilitati prima di chiamarlo), ma
    # qui resta allineato perche' i test leggano la stessa configurazione di
    # produzione.
    "enabled": True,
    "recalc": False,
    "min_signals": 2,
    "single_source_min": 40,
    "single_source_limit": 10,
    "shortlist_size": 25,
    "warn_institutional_age_days": 150,
    "windows": WINDOWS,
    "insider_scale": INSIDER_SCALE,
    "weights": WEIGHTS,
}
TODAY = date.today()
LAST_BAR = (TODAY - timedelta(days=2)).isoformat()
# L'eta' dei 13F si misura sulla data di segnale (l'ultima barra), non su oggi:
# e' cosi' che la description di uno stesso dataset non cambi da un giorno
# all'altro. I test sotto calcolano le date di deposito su questo riferimento.
ANCHOR = date.fromisoformat(LAST_BAR)


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


def add_insider(
    conn, company_id, *, kind="P", value=1_000_000, days_ago=1, open_market=1,
    shares=100, holdings_after=1_000, insider_name="TEST INSIDER", seq=0,
):
    """Il filing e' identificato da (accession, row_no): perche' una stessa
    azienda possa avere acquisto E vendita, l'accession include il tipo di
    transazione, altrimenti il secondo INSERT OR IGNORE verrebbe scartato in
    silenzio e il test passerebbe senza il contributo che crede di avere.

    `shares=100` su `holdings_after=1000` e' la quota neutra (10% dellaposizione)
    e restituisce il peso base: i test che vogliono provare la scala passano
    esplicitamente shares e holdings_after, quelli che vogliono solo sapere
    che il modulo ha parlato usano questi default.
    """
    conn.execute(
        """INSERT OR IGNORE INTO insider_transactions
             (company_id, accession, row_no, filing_date, transaction_date,
              insider_name, transaction_type, shares, price_per_share,
              value_usd, is_open_market, holdings_after)
           VALUES (?, ?, 1, ?, ?, ?, ?, ?, 10, ?, ?, ?)
        """,
        (
            company_id,
            f"{company_id:010d}-{kind}-{insider_name[:6]}-{days_ago:06d}-{seq:03d}",
            (TODAY - timedelta(days=days_ago)).isoformat(),
            (TODAY - timedelta(days=days_ago)).isoformat(),
            insider_name,
            kind,
            shares,
            value,
            open_market,
            holdings_after,
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


def add_holding(conn, company_id, *, quarter, filer_cik, cusip, shares, filer_name="TEST FUND",
                filing_date=None):
    conn.execute(
        """INSERT OR IGNORE INTO institutional_holdings
             (company_id, filing_quarter, filing_date, filer_name, filer_cik,
              issuer_name, cusip, shares, value_usd)
           VALUES (?, ?, ?, ?, ?, 'ISSUER', ?, ?, 1000)
        """,
        (company_id, quarter, filing_date or f"{quarter[:4]}-08-14", filer_name, filer_cik, cusip, shares),
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


def test_min_signals_splits_shortlist_in_two_sections_but_keeps_row(ctx_and_conn):
    """`min_signals` non e' piu' un filtro, e' un confine fra due sezioni.

    Entrambi i ticker hanno la riga: la shortlist e' un filtro di lettura, non di
    scrittura. SOLO (un modulo) va nella sezione fonte singola, DOPPIO (due
    moduli) in quella multipla, e la riga di SOLO porta l'etichetta che la Fase 7
    usa per separarle con una query.
    """
    ctx, conn, _path = ctx_and_conn
    solo = add_company(conn, "SOLO")
    add_insider(conn, solo, shares=1000, holdings_after=1000)      # +48, copertura 1/4
    doppio = add_company(conn, "DOPPIO")
    add_insider(conn, doppio, shares=1000, holdings_after=1000)    # +48
    add_news(conn, doppio, [0.9, 0.9, 0.9])                       # +20 -> 68, 2/4

    result = Module().run(ctx)
    # entrambi hanno la riga: la shortlist e' un filtro di lettura, non di scrittura
    assert composite(conn, "SOLO") is not None
    assert composite(conn, "DOPPIO") is not None
    assert "copertura 1/4" in composite(conn, "SOLO")["description"]
    assert "copertura 2/4" in composite(conn, "DOPPIO")["description"]

    desc_solo = composite(conn, "SOLO")["description"]
    assert SINGLE_SOURCE_LABEL in desc_solo, (
        "il fonte singola deve dichiararlo nella riga, non solo nella note"
    )
    assert SINGLE_SOURCE_LABEL not in composite(conn, "DOPPIO")["description"], (
        "un segnale confermato da due fonti non puo' portare l'etichetta del non confermato"
    )
    assert "in shortlist multipla=1" in result.note
    assert "in shortlist fonte singola=1" in result.note


def test_single_source_min_from_config_decides_section_two(ctx_and_conn):
    """La soglia arriva dalla config, non da una costante.

    Con `single_source_min` a 40 il SOLO da +30 resta fuori da ogni sezione:
    non e' rumore, ma non e' neppure un segnale forte, e promuoverlo a shortlist
    riempirebbe la lista di casi marginali. Alzando la soglia a 20 rientra, e
    questo dimostra che il filtro e' quello dichiarato e non altro.

    Sotto la soglia l'etichetta NON c'e' nemmeno: sotto `single_source_min` il
    ticker non e' un candidato della sezione 2, e scrivere "fonte singola" su una
    riga che nessuno listino' direbbe il vero a meta'. Resta invece etichettato
    se e' forte ma tagliato dal tetto di lista, perche' li' il segnale c'e'.
    """
    ctx, conn, _path = ctx_and_conn
    solo = add_company(conn, "SOLO")
    add_insider(conn, solo)                       # +30, copertura 1/4
    # Il secondo run cambia solo la soglia, non i dati: senza `recalc` la riga
    # scritta dal primo resterebbe quella vecchia (INSERT OR IGNORE) e il test
    # leggerebbe una description che nessuno codice ha più prodotto.
    ctx.module_config["recalc"] = True

    result = Module().run(ctx)
    assert "in shortlist fonte singola=0" in result.note
    assert SINGLE_SOURCE_LABEL not in composite(conn, "SOLO")["description"], (
        "sotto single_source_min non e' un candidato della sezione 2: etichettarlo "
        "qui direbbe il vero a meta'"
    )

    ctx.module_config["single_source_min"] = 20
    result = Module().run(ctx)
    assert "in shortlist fonte singola=1" in result.note
    assert "SOLO=+30" in result.note
    assert SINGLE_SOURCE_LABEL in composite(conn, "SOLO")["description"]

    # La soglia in note porta gia' il segno nel formato (`>=+20`): aggiungerne
    # un secondo a mano produceva `>=++20` in un report. Qui si blocca la
    # doppia soglia sul testo che finisce in tabella e nei log.
    assert "fonte singola=1 (>=+20)" in result.note
    assert "++" not in result.note


def test_single_source_limit_caps_section_two(ctx_and_conn):
    """Il tetto della sezione 2 vale solo per lei.

    Tre fonti singole forti e una multipla: la multipla deve restare in shortlist
    per intero, e le singole essere troncate al limite dichiarato. Se il tetto
    fosse applicato alla lista unica, un segnale confermato verrebbe escluso da
    un segnale non confermato piu' rumoreoso.
    """
    ctx, conn, _path = ctx_and_conn
    for i in range(3):
        add_insider(conn, add_company(conn, f"S{i}"), shares=1000, holdings_after=1000)  # +48
    multi = add_company(conn, "MULTI")
    add_insider(conn, multi)
    add_news(conn, multi, [0.9, 0.9, 0.9])

    ctx.module_config["single_source_limit"] = 1
    result = Module().run(ctx)

    assert "in shortlist multipla=1" in result.note
    assert "in shortlist fonte singola=1" in result.note
    assert "top multipla: MULTI=+50" in result.note
    # a parita' di score vince il ticker in ordine alfabetico: resta S0
    assert "top fonte singola: S0=" in result.note


def test_shortlist_sections_are_not_merged_into_one_ranking(ctx_and_conn):
    """Un +55 a fonte singola non e' piu' "convincente" di un +35 a due moduli.

    Le due sezioni restano due elenchi: se venissero accostate in un ranking
    unico, il segnale non confermato finirebbe davanti a quello confermato e il
    lettore prenderebbe per forte il dato piu' fragile del run.
    """
    ctx, conn, _path = ctx_and_conn
    # fonte singola forte: 30 x 1.6 (quota 100%) = 48
    forte = add_company(conn, "FORTE")
    add_insider(conn, forte, shares=1000, holdings_after=1000)
    # multipla PIU' DEBOLE: +4.5 (quota 0.47%, sotto la soglia trascurabile) + 20 news
    debole = add_company(conn, "DEBOLE")
    add_insider(conn, debole, shares=47, holdings_after=10_000)
    add_news(conn, debole, [0.9, 0.9, 0.9])

    result = Module().run(ctx)

    assert composite(conn, "FORTE")["magnitude"] == pytest.approx(48.0)
    assert composite(conn, "DEBOLE")["magnitude"] == pytest.approx(24.5)
    # 48 > 24.5: il fonte singola pesa di piu', e non sale lo stesso per questo
    assert "top multipla: DEBOLE=" in result.note
    assert "top fonte singola: FORTE=" in result.note
    # la copertura del singola resta quella dichiarata: non viene promosso
    assert "copertura 1/4" in composite(conn, "FORTE")["description"]
    assert "copertura 2/4" in composite(conn, "DEBOLE")["description"]
    assert SINGLE_SOURCE_LABEL in composite(conn, "FORTE")["description"]
    assert SINGLE_SOURCE_LABEL not in composite(conn, "DEBOLE")["description"]


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
    assert "in shortlist multipla=2" in result.note


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


def test_stale_institutional_still_contributes_with_its_age(ctx_and_conn):
    """Un 13F vecchio non e' un errore e non viene scartato: e' un fatto vero,
    solo vecchio, e il gate di freschezza e' stato tolto apposta. Quello che
    deve cambiare e' la DESCRIPTION, che porta l'eta' del deposito, e la nota
    del modulo che avvisa quando l'eta' supera la soglia. Se il 13F tornasse
    un errore, ogni run dopo la soglia segnalerebbe un problema che non
    esiste; se venisse scartato in silenzio, un modulo 13F fermo non si
    vedrebbe da nessuna parte."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100,
                filing_date="2020-01-15")
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=200,
                filing_date="2020-05-15")

    result = Module().run(ctx)
    conn.commit()
    assert [r["signal_type"] for r in contribution_rows(conn, "AAA")] == ["institutional_increase"]
    age = (ANCHOR - date(2020, 5, 15)).days
    assert "depositato 2020-05-15" in contribution_rows(conn, "AAA")[0]["description"]
    assert f"~{age}gg fa" in contribution_rows(conn, "AAA")[0]["description"]
    assert f"deposito piu' vecchio {age}gg" in result.note
    assert "potrebbe non aver depositato" in result.note
    # resta un'informazione, non un guasto: niente errori sul 13F
    assert all("institutional" not in e for e in result.errors), result.errors


def test_fresh_institutional_has_no_stale_warning(ctx_and_conn):
    """Il caso normale: deposito recente, il nome dei 13F non deve comparire
    nella nota. L'avviso che non c'e' quando non serve vale quanto l'avviso
    che c'e'."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    recent = (ANCHOR - timedelta(days=40)).isoformat()
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100,
                filing_date=recent)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=200,
                filing_date=recent)

    result = Module().run(ctx)
    assert "deposito piu' vecchio 40gg" in result.note
    assert "potrebbe non aver depositato" not in result.note


def test_institutional_age_comes_from_filing_date_not_quarter_end(ctx_and_conn):
    """Regressione: l'eta' si contava dalla FINE DEL TRIMESTRE e dichiarava
    ~96gg per una 13F depositata 51 giorni prima. Ora la riga riporta la data
    di deposito, che e' l'unica che descrive la freschezza del documento.
    """
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    filed = ANCHOR - timedelta(days=51)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=200,
                filing_date=filed.isoformat())

    Module().run(ctx)
    desc = contribution_rows(conn, "AAA")[0]["description"]
    assert f"depositato {filed.isoformat()}" in desc
    assert "~51gg fa" in desc


def test_institutional_age_is_per_row(ctx_and_conn):
    """Lo stesso trimestre arriva con due date di deposito diverse (emendamenti
    in momenti diversi): l'eta' va letta riga per riga, non calcolata una volta
    per il trimestre."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    primo = (ANCHOR - timedelta(days=51)).isoformat()
    secondo = (ANCHOR - timedelta(days=54)).isoformat()
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=200,
                filing_date=primo)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000002", cusip="BBB", shares=300,
                filing_date=secondo)

    result = Module().run(ctx)
    descs = [r["description"] for r in contribution_rows(conn, "AAA")]
    assert any(primo in d and "~51gg" in d for d in descs), descs
    assert any(secondo in d and "~54gg" in d for d in descs), descs
    # la nota prende il massimo: e' l'eta' peggiore che conta
    assert "deposito piu' vecchio 54gg" in result.note


def test_institutional_without_filing_date_says_so(ctx_and_conn):
    """Se la data di deposito manca non si inventa un'eta': si dichiara che non
    c'e'. Il peso del segnale e' invariato."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_price(conn, cid)
    add_holding(conn, cid, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=100,
                filing_date=None)
    add_holding(conn, cid, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=200,
                filing_date=None)
    conn.execute("UPDATE institutional_holdings SET filing_date = NULL WHERE company_id = ?", (cid,))
    conn.commit()

    Module().run(ctx)
    row = contribution_rows(conn, "AAA")[0]
    assert "data deposito n/d" in row["description"]
    assert row["magnitude"] == pytest.approx(15.0)


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
    assert "in shortlist multipla=2" in result.note
    # e l'ordine segue il punteggio, non l'etichetta: ACCO (+50) prima di CBRS
    assert result.note.index("ACCO=+50") < result.note.index("CBRS=+35")


# ?? 10. scala del contributo insider (frazione di posizione) ????????????????????
#
# I numeri qui sotto non sono arbitrari: sono i casi misurati sul DB di
# produzione del 2026-10-02, dove un peso fisso non distingueva una vendita del
# 61% della propria posizione da un taglio dello 0,6%.


def insider_weight(conn, ticker):
    rows = {r["signal_type"]: r for r in contribution_rows(conn, ticker)}
    return rows["insider_open_market_buy" if "insider_open_market_buy" in rows else "insider_open_market_sell"]


def test_insider_small_trade_is_downweighted(ctx_and_conn):
    """Caso KOD: 156.7M comprati ma 0,6% della posizione (taglio di routine).
    Con il peso fisso era +30, il massimo possibile per un insider.

    Oggi e' +4.5: 156.7M comprati ma 0.4% della posizione sono sotto la soglia
    trascurabile dell'1%. E' la conseguenza dichiarata della scelta "la quota
    vale il convinzione, il valore assoluto non entra": il caso resta coperto dal
    test perche' documenta il costo della scelta, non perche' lo approvi."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "KOD")
    add_insider(conn, cid, kind="P", value=156_700_000, shares=100, holdings_after=25_000)

    Module().run(ctx)
    row = insider_weight(conn, "KOD")
    assert row["magnitude"] == pytest.approx(4.5)      # 30 x 0.15 (banda trascurabile)
    assert "0.4% delle posizioni" in row["description"]
    assert "trascurabile" in row["description"]
    assert row["direction"] == 1


def test_insider_large_stake_is_amplified(ctx_and_conn):
    """Caso ADRX: 33.3M, ma 60,7% della posizione. Il peso fisso non lo
    distingueva da KOD, e il tetto a 45 lo clippava senza che nulla lo dicesse."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "ADRX")
    add_insider(conn, cid, kind="P", value=33_300_000, shares=607, holdings_after=1_000)

    Module().run(ctx)
    row = insider_weight(conn, "ADRX")
    assert row["magnitude"] == pytest.approx(48.0)     # 30 x 1.6, nessun clip
    assert "60.7% delle posizioni" in row["description"]


def test_insider_cap_holds_even_with_many_people(ctx_and_conn):
    """30 x 1.6 x 1.35 = 64.8 non e' piu' un segnale insider: il tetto esiste
    perche' i 4 moduli insieme arrivano a +100 e un singolo modulo non puo'
    dominare il totale. Con 60 il taglio c'e' ma non morde piu' nessun segnale
    reale: il massimo osservato era 55.2."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "BIG")
    for i in range(4):
        add_insider(conn, cid, kind="P", value=33_300_000, shares=607, holdings_after=1_000,
                    insider_name=f"INSIDER {i}", days_ago=i + 1)

    Module().run(ctx)
    row = insider_weight(conn, "BIG")
    assert row["magnitude"] == pytest.approx(60.0)     # 64.8 limitato a 60
    assert "4 insider distinti" in row["description"]


def test_insider_sell_fraction_uses_shares_over_pre_trade_total(ctx_and_conn):
    """In vendita la frazione non e' shares/holdings_after ma
    shares/(shares+holdings_after): `holdings_after` e' la posizione RESIDUA, e
    un rapporto sbagliato farebbe sembrare minuscola una vendita quasi totale."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "CX")
    add_insider(conn, cid, kind="S", value=8_900_000, shares=613, holdings_after=387)

    Module().run(ctx)
    row = insider_weight(conn, "CX")
    assert row["magnitude"] == pytest.approx(-32.0)    # -20 x 1.6
    assert row["direction"] == -1
    assert "61.3% delle posizioni" in row["description"]


def test_insider_full_exit_is_the_strongest_sell(ctx_and_conn):
    """`holdings_after = 0` e' l'uscita completa, non un dato mancante: e' la
    vendita piu' forte possibile e il peso piu' alto della banda. Se venisse
    trattata come NULL cadrebbe sul fallback per importo, cioe' il caso piu'
    forte pesato come il piu' debole."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "FULL")
    add_insider(conn, cid, kind="S", value=1_000_000, shares=1_000, holdings_after=0)

    Module().run(ctx)
    row = insider_weight(conn, "FULL")
    assert row["magnitude"] == pytest.approx(-32.0)
    assert "100.0% delle posizioni" in row["description"]
    assert "frazione n/d" not in row["description"]


def test_insider_fraction_is_weighted_by_value(ctx_and_conn):
    """La media e' pesata per valore: un ritaglio minuscolo da 90M non puo'
    contare quanto un'uscita completa da 1M. La media semplice dei due
    rapporti darebbe 10,5% (banda piena, -20)."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "MIX")
    add_insider(conn, cid, kind="S", value=1_000_000, shares=100, holdings_after=900,
                insider_name="PICCOLA", days_ago=1)
    add_insider(conn, cid, kind="S", value=90_000_000, shares=100, holdings_after=19_900,
                insider_name="GRANDE", days_ago=2)

    Module().run(ctx)
    row = insider_weight(conn, "MIX")
    # (1M x 10.0% + 90M x 0.5%) / 91M = 0.6% -> banda trascurabile;
    # due persone distinte aggiungono il fattore 1.15. Con la media semplice dei
    # due rapporti sarebbe 5.25% (banda piena, -23): e' la prova che la media
    # e' pesata per valore e non aritmetica.
    assert row["magnitude"] == pytest.approx(-3.45)     # -20 x 0.15 x 1.15
    assert "trascurabile" in row["description"]
    assert "0.6% delle posizioni" in row["description"]


def test_insider_counts_distinct_people_not_rows(ctx_and_conn):
    """Caso CBRS reale: 36 dichiarazioni di Form 4 da 3 persone. Contando le
    righe, 36 insider avrebbero fatto scattare la banda massima; sono 3."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "CBRS")
    for i in range(36):
        add_insider(conn, cid, kind="S", value=2_400_000, shares=100, holdings_after=900,
                    insider_name=f"DIRIGENTE {i % 3}", days_ago=i % 6 + 1, seq=i)

    Module().run(ctx)
    row = insider_weight(conn, "CBRS")
    assert "3 insider distinti su 36 dichiarazioni" in row["description"]
    assert row["magnitude"] == pytest.approx(-23.0)    # -20 x 1.15 (banda 2-3 persone)


def test_insider_count_bands_are_read_from_config(ctx_and_conn):
    """Una persona = 1.0, due = 1.15, quattro = 1.35: la differenza fra un
    singolo direttore e un nucleo di gestori che agiscono insieme."""
    ctx, conn, _path = ctx_and_conn
    for n, atteso in ((1, -20.0), (2, -23.0), (4, -27.0)):
        cid = add_company(conn, f"N{n}")
        for i in range(n):
            add_insider(conn, cid, kind="S", value=1_000_000, shares=100, holdings_after=900,
                        insider_name=f"DIR {n}-{i}", days_ago=i + 1)
    Module().run(ctx)
    for n, atteso in ((1, -20.0), (2, -23.0), (4, -27.0)):
        assert insider_weight(conn, f"N{n}")["magnitude"] == pytest.approx(atteso)


def test_insider_without_holdings_after_falls_back_to_value(ctx_and_conn):
    """Manca `holdings_after`: la frazione non e' calcolabile e il peso si prende
    dal valore in dollari. La description deve DIRLO, altrimenti sembrerebbe un
    segnale sulla quota di posizione quando la quota non e' mai stata calcolata.
    Caso reale: CBRS a 101K e a 87.7M erano entrambi -20."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "NOPCT")
    add_insider(conn, cid, kind="S", value=87_700_000, shares=None, holdings_after=None)

    Module().run(ctx)
    row = insider_weight(conn, "NOPCT")
    assert "frazione n/d (peso per valore)" in row["description"]
    assert row["magnitude"] == pytest.approx(-26.0)    # -20 x 1.30 (banda >= 50M)


def test_insider_fraction_coverage_is_declared(ctx_and_conn):
    """Se solo una parte del valore ha `holdings_after`, la frazione e' su quel
    sottoinsieme e la description dice quale: 8 dichiarazioni su 9 nel caso
    reale, quindi la quota e' comunque un'approssazione."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "PARZ")
    add_insider(conn, cid, kind="S", value=8_100_000, shares=810, holdings_after=90,
                insider_name="CON DATI", days_ago=1)
    add_insider(conn, cid, kind="S", value=900_000, shares=None, holdings_after=None,
                insider_name="SENZA DATI", days_ago=2)

    Module().run(ctx)
    desc = insider_weight(conn, "PARZ")["description"]
    assert "90.0% delle posizioni" in desc
    assert "su 90% del valore" in desc


def test_ceiling_band_marks_a_negligible_fraction_as_trascurabile(ctx_and_conn):
    """Sotto la soglia minima il contributo e' rumore, e lo dichiara.

    Caso reale AFL: una vendita dello 0.018% della posizione prendeva -14.0, lo
    stesso peso di un'uscita vera e piccola. Con la banda `lt` il peso scende a
    -3.0 e la description dice PERCHE', altrimenti un contributo piccolo senza
    spiegazione e' indistinguibile da un errore di calcolo.
    """
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AFL")
    add_insider(conn, cid, kind="S", value=1_000_000, shares=18, holdings_after=100_000)

    Module().run(ctx)
    riga = insider_weight(conn, "AFL")
    assert riga["magnitude"] == pytest.approx(-3.0), "sotto l'1% deve valere 20 x 0.15"
    assert "trascurabile" in riga["description"]


def test_ceiling_band_is_not_applied_above_the_threshold(ctx_and_conn):
    """La banda trascurabile ha un tetto, non una soglia.

    ABEO allo 1.501% e' una vendita reale e piccola: deve restare sulla banda
    normale. Se la banda `lt` fosse trattata come `gte` (o se il confronto fosse
    sbagliato), qui prenderebbe 0.15 e la distinzione che la modifica voleva
    conservare sparirebbe.
    """
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "ABEO")
    # 1501/(1501+100000) = 1.48%: sopra l'1%, quindi banda normale 0.7
    add_insider(conn, cid, kind="S", value=1_000_000, shares=1501, holdings_after=100_000)

    Module().run(ctx)
    riga = insider_weight(conn, "ABEO")
    assert riga["magnitude"] == pytest.approx(-14.0), "1.48% e' sopra l'1%: banda 0.7, non 0.15"
    assert "trascurabile" not in riga["description"]


def test_ceiling_band_boundary_is_exactly_at_the_configured_fraction(ctx_and_conn):
    """Il tetto e' STRETTO: esattamente l'1% e' gia' segnale.

    Il confine e' arbitrario in un senso e non nell'altro, e il codice deve
    dichiarare quale ha scelto: sotto `lt` il valore prende il tetto, alla soglia
    esatta prende la banda normale. Senza questo test un refactor da `<` a `<=`
    cambierebbe il peso di casi reali senza accorgersene.
    """
    ctx, conn, _path = ctx_and_conn
    sotto = add_company(conn, "SOTTO")
    # 999/(999+100000) = 0.99%
    add_insider(conn, sotto, kind="S", value=1_000_000, shares=999, holdings_after=100_000)
    uguale = add_company(conn, "UGUALE")
    # 1000/(1000+99000) = 1.00% esatto
    add_insider(conn, uguale, kind="S", value=1_000_000, shares=1000, holdings_after=99_000)

    Module().run(ctx)
    assert insider_weight(conn, "SOTTO")["magnitude"] == pytest.approx(-3.0)
    assert insider_weight(conn, "UGUALE")["magnitude"] == pytest.approx(-14.0)
    assert "trascurabile" in insider_weight(conn, "SOTTO")["description"]
    assert "trascurabile" not in insider_weight(conn, "UGUALE")["description"]


def test_ceiling_band_is_symmetric_between_buy_and_sell(ctx_and_conn):
    """33.7M di convinzione privata non dimostrano convinzione.

    Se la banda trascurabile valesse solo sulle vendite, un acquisto dello 0.5%
    continuerebbe a pesare come un acquisto vero: la simmetria scelta in config
    deve arrivare fino al contributo.
    """
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "CRBG")
    add_insider(conn, cid, kind="P", value=33_700_000, shares=470, holdings_after=100_000)

    Module().run(ctx)
    riga = insider_weight(conn, "CRBG")
    assert riga["magnitude"] == pytest.approx(4.5), "acquisto sotto l'1% vale 30 x 0.15"
    assert "trascurabile" in riga["description"]


def test_bands_without_ceiling_keep_the_previous_weights(ctx_and_conn):
    """Una config senza `lt` deve produrre esattamente i pesi di prima.

    La banda trascurabile e' un'aggiunta, non un cambio di contratto: se
    sparisse, ogni fixture e ogni run passato darebbero risultati diversi senza
    che nessuno lo abbia chiesto.
    """
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "OLD")
    add_insider(conn, cid, kind="S", value=1_000_000, shares=18, holdings_after=100_000)

    ctx.module_config["insider_scale"] = {
        **INSIDER_SCALE,
        "frac_sell": [
            {"gte": 0.50, "mult": 1.6}, {"gte": 0.20, "mult": 1.3},
            {"gte": 0.05, "mult": 1.0}, {"gte": 0.0, "mult": 0.7},
        ],
    }
    Module().run(ctx)
    riga = insider_weight(conn, "OLD")
    assert riga["magnitude"] == pytest.approx(-14.0), "senza 'lt' resta il vecchio 20 x 0.7"
    assert "trascurabile" not in riga["description"]


def test_ceiling_band_must_come_first_or_it_is_reported(ctx_and_conn):
    """Una banda `lt` non in testa e' una config che sembra funzionare e non
    fa niente: va in errors[], non accettata in silenzio.

    Sotto la soglia di un tetto precedente la banda successiva non viene mai
    valutata, quindi l'errore silenzioso sarebbe il caso peggiore: l'utente
    crederebbe di aver abbassato il rumore e non sarebbe successo nulla.
    """
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "BADLT")
    add_insider(conn, cid, kind="P", value=1_000_000)
    ctx.module_config["insider_scale"] = {
        **INSIDER_SCALE,
        "frac_buy": [{"gte": 0.5, "mult": 1.6}, {"lt": 0.01, "mult": 0.15}],
    }

    result = Module().run(ctx)
    assert any("banda non valida" in e and "prima" in e for e in result.errors), result.errors


def test_band_cannot_be_ceiling_and_floor_at_once(ctx_and_conn):
    """`gte` e `lt` sulla stessa banda e' una banda che non ha un significato:
    nessun numero la soddisferebbe, o tutti, a seconda di come si legge. Va
    segnalata invece di interpretata."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "BOTTA")
    add_insider(conn, cid, kind="P", value=1_000_000)
    ctx.module_config["insider_scale"] = {
        **INSIDER_SCALE,
        "frac_buy": [{"gte": 0.5, "lt": 0.1, "mult": 1.6}],
    }

    result = Module().run(ctx)
    assert any("banda non valida" in e and "lt" in e for e in result.errors), result.errors


def test_max_abs_from_config_caps_theory_but_not_real_signals(ctx_and_conn):
    """Il tetto assoluto deve tagliare il teorico e non i segnali reali.

    30 x 1.6 x 1.35 = 64.8 e' il massimo possibile e non deve arrivare intatto
    in tabella; ma un contributo da 55.2 (XENE, ADRX nella finestra reale) e' un
    segnale vero e con `max_abs: 45` spariva, clippato senza che nulla lo
    dichiarasse. Il tetto vale 60 proprio per questo.
    """
    ctx, conn, _path = ctx_and_conn
    teorico = add_company(conn, "TEOR")
    for i in range(4):     # 30 x 1.6 x 1.35 = 64.8, il massimo possibile
        add_insider(conn, teorico, kind="P", value=10_000_000, shares=1000,
                    holdings_after=1000, insider_name=f"INSIDER {i}", days_ago=i + 1)
    # una persona sola, quota 100%: 48.0, un segnale reale
    reale = add_company(conn, "REALE")
    add_insider(conn, reale, kind="P", value=10_000_000, shares=1000, holdings_after=1000)

    Module().run(ctx)
    assert insider_weight(conn, "TEOR")["magnitude"] == pytest.approx(60.0)
    assert insider_weight(conn, "REALE")["magnitude"] == pytest.approx(48.0)

    # e con il tetto a 45, che era il valore di prima: il segnale da 48 spariva.
    # Serve `recalc`, altrimenti la riga scritta col tetto a 60 resterebbe li' e
    # il test leggerebbe il valore vecchio senza accorgersene.
    ctx.module_config["recalc"] = True
    ctx.module_config["insider_scale"] = {**INSIDER_SCALE, "max_abs": 45}
    Module().run(ctx)
    assert insider_weight(conn, "REALE")["magnitude"] == pytest.approx(45.0)


def test_insider_bands_come_from_config(ctx_and_conn):
    """I pesi non sono scritti nel codice: cambiando la banda in config cambia
    il contributo."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "CFG")
    add_insider(conn, cid, kind="P", value=156_700_000, shares=100, holdings_after=25_000)

    ctx.module_config["insider_scale"] = {
        **INSIDER_SCALE,
        "frac_buy": [{"gte": 0.0, "mult": 0.5}],
    }
    Module().run(ctx)
    assert insider_weight(conn, "CFG")["magnitude"] == pytest.approx(15.0)   # 30 x 0.5


def test_broken_insider_band_is_reported_not_silent(ctx_and_conn):
    """Una banda malformata in config deve finire in errors[] con il nome della
    chiave: se il modulo lo ignora e restituisce zero contributi, il run sembra
    riuscito e il modulo insider sparisce senza dire niente."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "BAD")
    add_insider(conn, cid, kind="P", value=1_000_000)
    ctx.module_config["insider_scale"] = {**INSIDER_SCALE, "frac_buy": [{"gte": 0.5}]}

    result = Module().run(ctx)
    assert any("insider_trading" in e and "banda non valida" in e for e in result.errors), result.errors
    assert "insider_open_market_buy" not in [r["signal_type"] for r in contribution_rows(conn, "BAD")]


def test_same_dataset_produces_the_same_rows_twice(ctx_and_conn):
    """Regressione sulla riproducibilita'. Con le finestre ancorate a
    date('now') due run a poche ore di distanza sullo stesso identico dataset
    davano risultati diversi (misurato: un ticker entrava ed usciva dalla
    shortlist e la riga vecchia restava perche' INSERT OR IGNORE non la
    riscriveva). Il risultato deve essere funzione del DB, non dell'orologio."""
    ctx, conn, _path = ctx_and_conn
    for ticker, kind, age in (("AAA", "P", 1), ("BBB", "S", 3), ("CCC", "P", 6)):
        cid = add_company(conn, ticker)
        add_price(conn, cid)
        add_insider(conn, cid, kind=kind, value=40_000_000, shares=100, holdings_after=1_000,
                    days_ago=age)
        add_news(conn, cid, [0.9, 0.5, 0.2], days_ago=age)
    add_holding(conn, 1, quarter=PREV_QUARTER, filer_cik="0000000001", cusip="AAA", shares=10)
    add_holding(conn, 1, quarter=LATEST_QUARTER, filer_cik="0000000001", cusip="AAA", shares=20)

    Module().run(ctx)
    conn.commit()
    prima = [(r["signal_type"], r["magnitude"], r["direction"], r["description"])
             for r in conn.execute(
                 "SELECT signal_type, magnitude, direction, description FROM signals"
                 " WHERE module_key = 'scoring' ORDER BY company_id, signal_type").fetchall()]
    Module().run(ctx)
    conn.commit()
    dopo = [(r["signal_type"], r["magnitude"], r["direction"], r["description"])
            for r in conn.execute(
                "SELECT signal_type, magnitude, direction, description FROM signals"
                " WHERE module_key = 'scoring' ORDER BY company_id, signal_type").fetchall()]
    assert prima == dopo
    assert len(prima) > 0


def test_news_window_compares_datetimes_not_strings(ctx_and_conn):
    """`published_at` e' ISO con 'T' e offset, mentre `datetime()` produce
    'YYYY-MM-DD HH:MM:SS': confrontarle a parole funziona finche' le date
    differiscono, ma sullo stesso giorno 'T' > ' ' e un articolo delle 00:30
    entrava in una finestra che si chiudeva alle 12:00 dello stesso giorno.
    L'articolo di sotto e' 11 ore fuori finestra e porta un sentiment forte: se
    entrasse, la media dei 4 articoli sarebbe 0.0 e il segnale sparirebbe."""
    ctx, conn, _path = ctx_and_conn
    cid = add_company(conn, "AAA")
    add_news(conn, cid, [0.9, 0.9, 0.9], days_ago=0)
    soglia = TODAY - timedelta(days=14)
    conn.execute(
        "INSERT INTO news_events (company_id, uuid, published_at, title, sentiment_score)"
        " VALUES (?, 'fuori-finestra', ?, 'fuori finestra', -0.9)",
        (cid, f"{soglia.isoformat()}T00:30:00+00:00"),
    )
    conn.commit()

    Module().run(ctx)
    rows = [r for r in contribution_rows(conn, "AAA") if r["module_key"] == "news_sentiment"]
    assert [r["signal_type"] for r in rows] == ["news_sentiment_positive"], [
        r["description"] for r in rows
    ]
    assert "su 3 articoli" in rows[0]["description"]
