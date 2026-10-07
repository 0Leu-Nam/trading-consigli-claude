"""Test end-to-end del modulo insider_trading su DB temporaneo (rete mockata)."""

import sqlite3

from core import db
from core.module_interface import RunContext
from modules.insider_trading import sec_edgar
from modules.insider_trading.module import Module

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument xmlns="http://www.sec.gov/edgar/ownership">
  <schemaVersion>X0507</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-09-15</periodOfReport>
  <issuer>
    <issuerCik>0000789019</issuerCik>
    <issuerName>ACME CORP</issuerName>
    <issuerTradingSymbol>ACME</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0000123456</rptOwnerCik><rptOwnerName>Mario Rossi</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>true</isDirector><isOfficer>true</isOfficer><officerTitle>President</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-18</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>25.5</value></transactionPricePerShare>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-19</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><value>26.0</value></transactionPricePerShare>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>4800</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


class _Resp:
    def __init__(self, content: bytes):
        self.content = content


def _make_discovery(*accessions):
    return [
        sec_edgar.FilingDiscovery(accession=a, filing_date="2026-09-16", ciks=["0000789019"])
        for a in accessions
    ]


def _run_module(tmp_path, config_overrides: dict | None = None) -> tuple[Module, RunContext, sqlite3.Connection, str]:
    path = tmp_path / "app.db"
    db.init_schema(path)
    conn = db.connect(path)

    module_config = {
        "lookback_days": 7,
        "min_value_usd": 0,
        "open_market_only": False,
        "max_filings": 5,
    }
    module_config.update(config_overrides or {})
    ctx = RunContext(conn=conn, module_config=module_config, global_config={}, env_getter=lambda k, d=None: d)
    return Module(), ctx, conn, str(path)


def test_module_inserts_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_edgar, "search_form4", lambda *a, **k: _make_discovery("ACC-1"))
    monkeypatch.setattr(sec_edgar, "fetch_ownership_xml_url", lambda *a, **k: "https://x/ownership.xml")
    monkeypatch.setattr(sec_edgar, "_get", lambda *a, **k: _Resp(SAMPLE_XML.encode()))

    module, ctx, conn, path = _run_module(tmp_path)
    r1 = module.run(ctx)
    conn.commit()
    assert r1.status == "ok"
    assert r1.rows_written == 2

    r2 = module.run(ctx)
    conn.commit()
    assert r2.rows_written == 0  # già presenti: INSERT OR IGNORE non fa nulla

    with db.connect(path) as c:
        n = c.execute("SELECT COUNT(*) AS n FROM insider_transactions").fetchone()["n"]
        assert n == 2
        company = c.execute("SELECT ticker, name, cik FROM companies").fetchone()
        assert company["ticker"] == "ACME"
        assert company["name"] == "ACME CORP"
    assert r1.watermark is not None  # module_state viene aggiornato dall'orchestratore
    conn.close()


def test_module_min_value_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_edgar, "search_form4", lambda *a, **k: _make_discovery("ACC-2"))
    monkeypatch.setattr(sec_edgar, "fetch_ownership_xml_url", lambda *a, **k: "https://x/ownership.xml")
    monkeypatch.setattr(sec_edgar, "_get", lambda *a, **k: _Resp(SAMPLE_XML.encode()))

    module, ctx, conn, path = _run_module(tmp_path, {"min_value_usd": 60000})
    r = module.run(ctx)
    conn.commit()
    assert r.rows_written == 0  # 25.5k e 5.2k entrambi sotto soglia
    conn.close()


def test_find_and_findall_tolerate_none():
    assert sec_edgar._find(None, "issuer") is None
    assert sec_edgar._findall(None, "nonDerivativeTransaction") == []


def test_parse_form4_tolerates_missing_fields():
    """Form 4 con campi assenti lungo la catena: non deve sollevare AttributeError."""
    removals = {
        "transactionAmounts": """      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>25.5</value></transactionPricePerShare>
      </transactionAmounts>
""",
        "postTransactionAmounts": """      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
""",
        "transactionCoding": """      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
""",
        "sharesOwnedFollowingTransaction": """<sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>""",
    }
    for name, block in removals.items():
        xml_bytes = SAMPLE_XML.replace(block, "").encode()
        ticker, company_name, cik, transactions = sec_edgar.parse_form4(xml_bytes, "https://x/o.xml", "ACC-X")
        assert ticker == "ACME"
        assert company_name == "ACME CORP"
        assert isinstance(transactions, list)


def test_parse_form4_missing_transaction_coding_skips_row():
    """Senza transactionCoding la riga viene scartata (non crasha)."""
    xml_bytes = SAMPLE_XML.replace(
        "      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>\n", ""
    ).encode()
    ticker, _, _, transactions = sec_edgar.parse_form4(xml_bytes, "https://x/o.xml", "ACC-X")
    assert [t.transaction_type for t in transactions] == ["S"]  # resta solo la seconda


def test_parse_form4_missing_amounts_leaves_none_but_keeps_row():
    xml_bytes = SAMPLE_XML.replace(
        """      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>25.5</value></transactionPricePerShare>
      </transactionAmounts>
""",
        "",
    ).encode()
    ticker, _, _, transactions = sec_edgar.parse_form4(xml_bytes, "https://x/o.xml", "ACC-X")
    assert len(transactions) == 2
    first = next(t for t in transactions if t.shares is None)
    assert first.transaction_type == "P"
    assert first.value_usd is None


def test_parse_form4_missing_post_amounts_leaves_holdings_none():
    xml_bytes = SAMPLE_XML.replace(
        "      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>\n",
        "",
    ).encode()
    ticker, _, _, transactions = sec_edgar.parse_form4(xml_bytes, "https://x/o.xml", "ACC-X")
    assert transactions[0].holdings_after is None


def test_parse_form4_missing_shares_owned_leaves_holdings_none():
    """Manca sharesOwnedFollowingTransaction ma resta postTransactionAmounts."""
    xml_bytes = SAMPLE_XML.replace(
        "<sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>", ""
    ).encode()
    ticker, _, _, transactions = sec_edgar.parse_form4(xml_bytes, "https://x/o.xml", "ACC-X")
    assert transactions[0].holdings_after is None


def test_module_isolates_partial_failure_in_errors(tmp_path, monkeypatch):
    """Un filing malformato finisce in errors[], non fa fallire l'intero modulo (run -> warning)."""
    monkeypatch.setattr(sec_edgar, "search_form4", lambda *a, **k: _make_discovery("ACC-OK", "ACC-BAD"))

    def fake_fetch(_user_agent, discovery):
        return f"https://x/{discovery.accession}.xml"

    def fake_get(url, *a, **k):
        return _Resp(SAMPLE_XML.encode() if url.endswith("ACC-OK.xml") else b"<ownershipDocument>")

    monkeypatch.setattr(sec_edgar, "fetch_ownership_xml_url", fake_fetch)
    monkeypatch.setattr(sec_edgar, "_get", fake_get)

    module, ctx, conn, path = _run_module(tmp_path)
    r = module.run(ctx)
    conn.commit()
    assert r.status == "ok"  # il modulo sopravvive: l'errore è parziale
    assert any("ACC-BAD" in err for err in r.errors)
    assert r.rows_written == 2  # il filing valido è stato processato

    # lo scheletro dell'orchestratore classifica gli errori parziali come warning
    from core.orchestrator import classify_run

    from core.module_interface import ModuleResult

    assert classify_run([r]) == "warning"
    conn.close()