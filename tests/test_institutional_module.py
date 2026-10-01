"""Test end-to-end del modulo institutional_holdings su DB temporaneo (rete mockata)."""

import json
from datetime import date

import pytest

from core import db
from core.module_interface import RunContext
from modules.institutional_holdings import cusip_map, edgar_13f
from modules.institutional_holdings import module as module_mod
from modules.institutional_holdings.module import Module, target_quarters

TABLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable>
  <infoTable>
    <nameOfIssuer>ABBVIE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>00287Y109</cusip><value>571474</value>
    <shrsOrPrnAmt><sshPrnamt>2271</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>1540000</value>
    <shrsOrPrnAmt><sshPrnamt>9000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>5000</value>
    <shrsOrPrnAmt><sshPrnamt>30</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>CALL</putCall>
  </infoTable>
  <infoTable>
    <nameOfIssuer>ACME WIDGETS LTD</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>XXXXXX999</cusip><value>1000</value>
    <shrsOrPrnAmt><sshPrnamt>50</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>
"""

TICKER_MAP_PAYLOAD = {
    "0": {"cik_str": 1551152, "ticker": "ABBV", "title": "AbbVie Inc."},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}

DEFAULT_CONFIG = {
    "lag_days": 45,
    "quarters_back": 1,
    "max_filings": 120,
    "max_filings_scan": 2000,
    "skip_put_call": True,
    "cusip_ttl_days": 60,
    "filer_cik_filter": [],
}


class _Resp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content
        self.text = content.decode(errors="ignore")

    def json(self):
        if self._payload is None:
            raise ValueError("non json")
        return self._payload


def _filing(accession="0000000001-26-000001", filer_cik="0000000001", name="ACME CAPITAL"):
    return edgar_13f.Filing13F(
        accession=accession, file_date="2026-08-01", period_ending="2026-06-30",
        ciks=[filer_cik], display_names=[name],
    )


@pytest.fixture()
def ctx_and_conn(tmp_path):
    path = tmp_path / "app.db"
    db.init_schema(path)
    conn = db.connect(path)
    ctx = RunContext(conn=conn, module_config=dict(DEFAULT_CONFIG), global_config={},
                     env_getter=lambda k, d=None: d)
    yield ctx, conn, path
    conn.close()


def _mock_all(monkeypatch, filings=None):
    monkeypatch.setattr(edgar_13f, "search_13f", lambda *a, **k: list(filings or [_filing()]))
    monkeypatch.setattr(edgar_13f, "discover_information_table", lambda *a, **k: "https://x/info.xml")
    monkeypatch.setattr(edgar_13f, "_get", lambda *a, **k: _Resp(content=TABLE_XML))
    monkeypatch.setattr(cusip_map, "_get", lambda *a, **k: _Resp(payload=TICKER_MAP_PAYLOAD))
    monkeypatch.setattr(cusip_map, "_MAP_TTL_SECONDS", 0)


def test_inserts_resolved_rows_and_reports_skips(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)
    result = Module().run(ctx)
    conn.commit()

    assert result.status == "ok"
    assert result.rows_written == 2  # ABBVIE + APPLE (CALL scartata, ACME non risolto)
    assert "cusip non risolti=1" in result.note
    assert "opzioni scartate=1" in result.note
    assert result.watermark == date.today().isoformat()

    quarter = target_quarters(date.today(), 45, 1)[0].quarter
    row = conn.execute(
        """SELECT filing_quarter, filing_date, filer_name, filer_cik, issuer_name,
                  cusip, shares, value_usd, shares_delta
           FROM institutional_holdings WHERE cusip = '00287Y109'"""
    ).fetchone()
    assert row["filing_quarter"] == quarter
    assert row["filing_date"] == "2026-08-01"
    assert row["filer_name"] == "ACME CAPITAL"
    assert row["filer_cik"] == "0000000001"
    assert row["issuer_name"] == "ABBVIE INC"
    assert row["shares"] == 2271
    assert row["value_usd"] == 571474  # grezzo
    assert row["shares_delta"] is None  # primo trimestre osservato


def test_unresolved_cusip_is_tracked_but_not_inserted(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)
    Module().run(ctx)
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM institutional_holdings WHERE cusip = 'XXXXXX999'"
    ).fetchone()["n"] == 0
    tracked = conn.execute(
        "SELECT company_id, resolved_at FROM cusip_lookup WHERE cusip = 'XXXXXX999'"
    ).fetchone()
    assert tracked["company_id"] is None
    assert tracked["resolved_at"] is None


def test_second_run_is_idempotent(ctx_and_conn, monkeypatch):
    ctx, conn, path = ctx_and_conn
    _mock_all(monkeypatch)
    first = Module().run(ctx)
    conn.commit()
    second = Module().run(ctx)
    conn.commit()
    assert first.rows_written == 2
    assert second.rows_written == 0
    with db.connect(path) as check:
        assert check.execute("SELECT COUNT(*) AS n FROM institutional_holdings").fetchone()["n"] == 2


def test_partial_error_does_not_fail_the_run(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    good = _filing(accession="0000000001-26-000001", filer_cik="0000000001")
    bad = _filing(accession="0000000002-26-000002", filer_cik="0000000002", name="KO CAPITAL")
    _mock_all(monkeypatch, filings=[good, bad])

    def fake_discover(_ua, accession, _ciks):
        if accession == bad.accession:
            raise edgar_13f.SecEdgarError("timeout")
        return "https://x/info.xml"

    monkeypatch.setattr(edgar_13f, "discover_information_table", fake_discover)
    result = Module().run(ctx)
    conn.commit()
    assert result.status == "ok"
    assert result.rows_written == 2              # il filing buono ha prodotto i suoi 2 titoli
    assert len(result.errors) == 1
    assert bad.accession in result.errors[0]
    assert "errori parziali=1" in result.note


def test_search_failure_is_a_hard_error(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)

    def boom(*a, **k):
        raise edgar_13f.SecEdgarError("EFTS non raggiungibile")

    monkeypatch.setattr(edgar_13f, "search_13f", boom)
    result = Module().run(ctx)
    assert result.status == "error"
    assert result.rows_written == 0
    assert "ricerca EFTS fallita" in result.errors[0]


def test_whitelist_filer_is_processed_before_others(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    other = _filing(accession="0000000009-26-000009", filer_cik="0000000009", name="OTHER FUND")
    wanted = _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND")
    _mock_all(monkeypatch, filings=[other, wanted])
    ctx.module_config["max_filings"] = 1
    ctx.module_config["filer_cik_filter"] = [123]  # CIK breve: normalizzato a 10 cifre

    result = Module().run(ctx)
    conn.commit()
    filer = conn.execute("SELECT DISTINCT filer_cik FROM institutional_holdings").fetchone()
    assert filer["filer_cik"] == "0000000123"
    assert "whitelist=1" in result.note
    assert result.rows_written == 2  # budget 1 filing → 2 titoli risolti


def test_shares_delta_is_computed_against_previous_quarter(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    windows = target_quarters(date.today(), 45, 2)
    company_id = db.upsert_company(conn, "ABBV", name="ABBVIE INC", cik="0001551152")
    conn.execute(
        """INSERT INTO institutional_holdings
             (company_id, filing_quarter, filer_cik, cusip, issuer_name, shares, value_usd)
           VALUES (?, ?, '0000000001', '00287Y109', 'ABBVIE INC', 2000, 400000)""",
        (company_id, windows[1].quarter),
    )
    conn.commit()

    _mock_all(monkeypatch)
    Module().run(ctx)
    conn.commit()
    row = conn.execute(
        "SELECT shares_delta FROM institutional_holdings WHERE cusip = '00287Y109' AND filing_quarter = ?",
        (windows[0].quarter,),
    ).fetchone()
    assert row["shares_delta"] == 271  # 2271 - 2000


def test_put_call_rows_can_be_kept_if_configured(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    ctx.module_config["skip_put_call"] = False
    _mock_all(monkeypatch)
    result = Module().run(ctx)
    conn.commit()
    # La CALL su APPLE ha lo stesso CUSIP della posizione azionaria: la UNIQUE
    # (filing_quarter, filer_cik, cusip) la ignora, quindi resta 2 righe.
    assert result.rows_written == 2
    assert "opzioni scartate=0" in result.note


def test_principal_amount_rows_are_skipped(ctx_and_conn, monkeypatch):
    # Obbligazioni/fondi (sshPrnamtType=PRN) non sono posizioni azionarie.
    ctx, conn, _path = ctx_and_conn
    bond_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <informationTable>
      <infoTable>
        <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>DEBT</titleOfClass>
        <cusip>037833100</cusip><value>999</value>
        <shrsOrPrnAmt><sshPrnamt>777</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
      <infoTable>
        <nameOfIssuer>ABBVIE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
        <cusip>00287Y109</cusip><value>571474</value>
        <shrsOrPrnAmt><sshPrnamt>2271</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
    </informationTable>"""
    _mock_all(monkeypatch)
    monkeypatch.setattr(edgar_13f, "_get", lambda *a, **k: _Resp(content=bond_xml))

    result = Module().run(ctx)
    conn.commit()
    assert "non-SH scartate=1" in result.note
    assert result.rows_written == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM institutional_holdings WHERE cusip = '037833100'"
    ).fetchone()["n"] == 0


def test_filing_with_foreign_period_is_skipped(ctx_and_conn, monkeypatch):
    # Caso reale osservato: un 13F depositato ad agosto con period_ending
    # 2024-12-31. Non deve essere etichettato come 2026Q2.
    ctx, conn, _path = ctx_and_conn
    stale = _filing(accession="0000000007-26-000007", filer_cik="0000000007",
                     name="STALE FUND")
    stale.period_ending = "2024-12-31"
    _mock_all(monkeypatch, filings=[stale])

    result = Module().run(ctx)
    conn.commit()
    assert result.rows_written == 0
    assert "periodo fuori finestra=1" in result.note
    assert conn.execute("SELECT COUNT(*) AS n FROM institutional_holdings").fetchone()["n"] == 0
    # Il CUSIP non viene nemmeno messo in cache: il filing è ignorato intero.
    assert conn.execute("SELECT COUNT(*) AS n FROM cusip_lookup").fetchone()["n"] == 0


def test_period_matches_quarter_helper():
    assert module_mod.period_matches_quarter("2026-06-30", "2026Q2") is True
    assert module_mod.period_matches_quarter("2026-03-31", "2026Q1") is True
    assert module_mod.period_matches_quarter("2024-12-31", "2026Q2") is False
    assert module_mod.period_matches_quarter("", "2026Q2") is True          # ignoto → accetta
    assert module_mod.period_matches_quarter("non-una-data", "2026Q2") is True


def test_unexpected_row_error_does_not_fail_the_filing(ctx_and_conn, monkeypatch):
    # Un errore non previsto sul singolo CUSIP (es. rete/DB) non deve far
    # fallire il run: va in errors[] e gli altri titoli procedono.
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)
    calls = {"n": 0}
    real_resolve = cusip_map.resolve

    def flaky_resolve(user_agent, connection, cusip, issuer_name, **kwargs):
        calls["n"] += 1
        if cusip == "00287Y109":
            raise RuntimeError("boom nel resolver")
        return real_resolve(user_agent, connection, cusip, issuer_name, **kwargs)

    monkeypatch.setattr(cusip_map, "resolve", flaky_resolve)
    result = Module().run(ctx)
    conn.commit()
    assert result.status == "ok"
    assert result.rows_written == 1          # solo APPLE, ABBVIE è fallito
    assert any("boom nel resolver" in err for err in result.errors)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM institutional_holdings WHERE cusip = '00287Y109'"
    ).fetchone()["n"] == 0