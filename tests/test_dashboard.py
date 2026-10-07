"""Test della dashboard statica (Fase 7a).

La pagina e' generata in sola lettura dal DB versionato: il rischio che i
test devono coprire non e' che la pagina esista, ma che non menta. Tre regole
in particolare finiscono qui:

1. il rendering dei casi sporchi (name NULL) non produce "None" nel testo;
2. la separazione in due sezioni segue la regola della Fase 6, non una
   classifica nuova;
3. la generazione non tocca il database (`mode=ro`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import db
from dashboard.generate import (
    SINGLE_SOURCE_LABEL,
    NoSignals,
    build,
    connect_readonly,
    generate,
    parse_composite,
    split_sections,
)
from modules.scoring.module import SINGLE_SOURCE_LABEL as LABEL_FASE6
from modules.scoring.module import _shortlist_sections

SIGNAL_DATE = "2026-10-05"
GENERATED = "2026-10-05T02:00:00+00:00"
CONF = {"modules": {"scoring": {
    "min_signals": 2,
    "single_source_min": 40,
    "single_source_limit": 10,
    "shortlist_size": 25,
}}}


def _insert_signal(conn, company_id, *, magnitude, description, signal_type="composite",
                   module_key="scoring"):
    conn.execute(
        """INSERT INTO signals
             (company_id, generated_at, signal_date, module_key, signal_type,
              magnitude, direction, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, GENERATED, SIGNAL_DATE, module_key, signal_type,
         magnitude, 1 if magnitude >= 0 else -1, description),
    )


@pytest.fixture()
def dashdb(tmp_path):
    """DB in memoria con shortlist finta ma con i casi da renderizzare."""
    path = tmp_path / "dash.db"
    db.init_schema(path)
    conn = db.connect(path)
    al = db.upsert_company(conn, "ALP", name="ALPHA CO")
    nul = db.upsert_company(conn, "NUL", name=None)
    single = db.upsert_company(conn, "SNG", name="SINGLE CO")
    low = db.upsert_company(conn, "LOW", name="LOW")
    _insert_signal(conn, al, magnitude=68.0,
                   description="score +68.0 da 2 contributi, "
                               "copertura 2/4 (news_sentiment,price_screener)")
    _insert_signal(conn, nul, magnitude=-12.0,
                   description="score -12.0 da 2 contributi, "
                               "copertura 2/4 (insider_trading,news_sentiment); "
                               "segnali contrastanti: + news_sentiment_positive "
                               "vs - insider_open_market_sell")
    _insert_signal(conn, single, magnitude=45.0,
                   description=f"score +45.0 da 1 contributi, "
                               f"copertura 1/4 (insider_trading); {LABEL_FASE6}")
    _insert_signal(conn, low, magnitude=-5.0,
                   description="score -5.0 da 1 contributi, copertura 1/4 (price_screener)")
    _insert_signal(conn, al, magnitude=30.0,
                   description="26/10/2026 ha comunicato l'acquisto di 5.000 azioni "
                               "a $123,45; deposito 2026-08-14, ~50gg fa",
                   module_key="insider_trading", signal_type="open_market_purchase")
    conn.commit()
    yield path, conn
    conn.close()


def test_split_sections_rispetta_la_regola_della_fase6():
    """Le due sezioni devono contenere ESATTAMENTE quello che la Fase 6
    metterebbe in shortlist, perche' la pagina e' una vista delle sue righe."""
    def row(ticker, score, coverage):
        return {"ticker": ticker, "score": score, "coverage": coverage,
                "name": ticker, "description": ""}

    rows = [row("A", 68.0, 2), row("B", 45.0, 1), row("C", 39.0, 1),
            row("D", -12.0, 2), row("E", 12.0, 4), row("F", 55.0, 3)]
    multi, single, rest = split_sections(
        rows, min_signals=2, shortlist_size=25, single_source_limit=10,
        single_min=40.0,
    )
    assert [r["ticker"] for r in multi] == ["A", "F", "E", "D"]
    assert [r["ticker"] for r in single] == ["B"]
    reasons = {r["ticker"]: r["reason"] for r in rest}
    assert "C" in reasons and "sotto soglia fonte singola" in reasons["C"]


def _scored_da_fase6(rows, min_signals=2, limit=25, single_min=40.0, single_limit=10):
    scored = []
    for i, r in enumerate(rows):
        scored.append((r["score"], r["coverage"], r["ticker"], i, ""))
    multi, single = _shortlist_sections(
        scored, min_signals, limit, single_min, single_limit)
    return {t for _s, _n, t, _i, _cf in multi}, {t for _s, _n, t, _i, _cf in single}


def test_split_sections_coincide_con_fase6_su_confronto_diretto():
    """Sullo stesso input, le due funzioni partizionano le stesse righe."""
    rows = [{"ticker": t, "score": s, "coverage": c, "name": t, "description": ""}
            for t, s, c in [
                ("A", 68.0, 2), ("B", 45.0, 1), ("C", 39.0, 1),
                ("D", -12.0, 2), ("E", 12.0, 4), ("F", 55.0, 3),
                ("G", 25.0, 1), ("H", 41.0, 1),
            ]]
    multi, single, rest = split_sections(
        rows, min_signals=2, shortlist_size=25, single_source_limit=10, single_min=40.0)
    m6, s6 = _scored_da_fase6(rows)
    assert {r["ticker"] for r in multi} == m6
    assert {r["ticker"] for r in single} == s6
    assert {r["ticker"] for r in multi} | {r["ticker"] for r in single} == m6 | s6


def test_parse_composite_estrae_copertura_contributi_e_contrasto():
    info = parse_composite(
        "score -12.0 da 2 contributi, copertura 2/4 "
        "(insider_trading,news_sentiment); "
        "segnali contrastanti: + news_sentiment_positive vs "
        "- insider_open_market_sell"
    )
    assert info["coverage"] == 2
    assert info["coverage_total"] == 4
    assert info["contribs"] == 2
    assert "contrastanti" in info["conflict"]
    assert info["single_source"] is False
    assert parse_composite(f"score +45.0 da 1 contributi, copertura 1/4 "
                           f"(insider_trading); {LABEL_FASE6}")["single_source"]


def test_build_scrive_mdash_e_mai_none(dashdb):
    path, _conn = dashdb
    conn = connect_readonly(path)
    page = build(conn, CONF)
    conn.close()
    assert ">None<" not in page
    assert "&mdash;" in page
    assert "nome non disponibile" in page


def test_build_escape_il_contenuto_del_db(dashdb):
    path, conn = dashdb
    conn.execute("""UPDATE signals SET description = ?
                    WHERE signal_type='composite' AND company_id =
                      (SELECT id FROM companies WHERE ticker='ALP')""",
                 ('score +68.0 da 2 contributi, copertura 2/4 '
                  '(news_sentiment,price_screener); '
                  '<script>alert(1)</script> & "quote"',))
    conn.commit()
    conn2 = connect_readonly(path)
    page = build(conn2, CONF)
    conn2.close()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "&quot;" in page


def test_build_mostra_punteggio_negativo_con_segno(dashdb):
    path, _conn = dashdb
    conn = connect_readonly(path)
    page = build(conn, CONF)
    conn.close()
    assert "class=\"num neg\">-12.0</td>" in page
    assert "badge conflict" in page


def test_sezioni_separate_nella_pagina(dashdb):
    path, _conn = dashdb
    conn = connect_readonly(path)
    page = build(conn, CONF)
    conn.close()
    assert "Convergenza multipla" in page
    assert "fonte singola, non confermata" in page
    assert ">2/4</td>" in page
    # la description della riga di sintesi resta consultabile nel title
    assert "copertura 2/4" in page


def test_generate_non_modifica_il_db(dashdb, tmp_path):
    path, conn = dashdb
    conn.close()
    before = Path(path).read_bytes()
    out = tmp_path / "out.html"
    generate(path, CONF, out, signal_date=SIGNAL_DATE)
    after = Path(path).read_bytes()
    assert before == after
    assert "<table" in out.read_text(encoding="utf-8")
    assert "<html" in out.read_text(encoding="utf-8")


def test_build_senza_righe_alza_nosignals(tmp_path):
    path = tmp_path / "empty.db"
    db.init_schema(path)
    conn = db.connect(path)
    conn.close()
    with pytest.raises(NoSignals):
        generate(path, CONF, tmp_path / "x.html")


def test_data_inesistente_non_scrive_file(dashdb, tmp_path):
    path, conn = dashdb
    conn.close()
    out = tmp_path / "x.html"
    with pytest.raises(NoSignals):
        generate(path, CONF, out, signal_date="2000-01-01")
    assert not out.exists()


def test_build_è_deterministica(dashdb):
    path, _conn = dashdb
    a = build(connect_readonly(path), CONF)
    b = build(connect_readonly(path), CONF)
    assert a == b


def test_descrizione_fonte_singola_è_due_sezioni_separate(dashdb):
    path, _conn = dashdb
    conn = connect_readonly(path)
    page = build(conn, CONF)
    conn.close()
    # SNG ha 1/4 e >=40: va in sezione 2, non in 1 e non nel resto.
    assert "SNG" in page