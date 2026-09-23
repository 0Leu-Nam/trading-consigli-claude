"""Test del parser Form 4 SEC EDGAR (senza rete)."""

import modules.insider_trading.sec_edgar as sec

FORM4_XML = """<?xml version="1.0" encoding="UTF-8"?>
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
    <reportingOwnerId>
      <rptOwnerCik>0000123456</rptOwnerCik>
      <rptOwnerName>Mario Rossi</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>true</isDirector>
      <isOfficer>true</isOfficer>
      <officerTitle>President</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-09-18</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>false</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>25.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-09-19</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>S</transactionCode>
        <equitySwapInvolved>false</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><value>26.0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>4800</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_form4_with_namespace():
    url = "https://www.sec.gov/Archives/edgar/data/0000789019/0000789019-26-000001/ownership.xml"
    ticker, name, cik, rows = sec.parse_form4(FORM4_XML.encode(), url, "0000789019-26-000001")
    assert ticker == "ACME"
    assert name == "ACME CORP"
    assert cik == "0000789019"
    assert len(rows) == 2
    p, s = rows

    assert p.accession == "0000789019-26-000001"
    assert p.row_no == 0
    assert p.transaction_type == "P"
    assert p.shares == 1000
    assert p.price_per_share == 25.5
    assert p.value_usd == 25500
    assert p.holdings_after == 5000
    assert p.is_open_market is True
    assert p.insider_name == "Mario Rossi"
    assert p.insider_title == "President"
    assert p.transaction_date == "2026-09-18"

    assert s.transaction_type == "S"
    assert s.value_usd == 5200
    assert s.row_no == 1
    assert s.is_open_market is True  # anche le vendite sul mercato sono "open market"


def test_parse_form4_ignores_unknown_codes():
    xml = FORM4_XML.replace("<transactionCode>P</transactionCode>", "<transactionCode>ZZ</transactionCode>")
    _ticker, _name, _cik, rows = sec.parse_form4(xml.encode(), "http://x", "ACC-1")
    assert len(rows) == 1
    assert rows[0].transaction_type == "S"


def test_parse_form4_without_namespace():
    nos = FORM4_XML.replace(' xmlns="http://www.sec.gov/edgar/ownership"', "")
    _ticker, _name, _cik, rows = sec.parse_form4(nos.encode(), "http://x", "ACC-2")
    assert len(rows) == 2


def test_parse_form4_missing_optional_price():
    xml = FORM4_XML.replace("<transactionPricePerShare><value>25.5</value></transactionPricePerShare>",
                            "<transactionPricePerShare></transactionPricePerShare>")
    _ticker, _name, _cik, rows = sec.parse_form4(xml.encode(), "http://x", "ACC-3")
    assert rows[0].value_usd is None
    assert rows[0].is_open_market is False