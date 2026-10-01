"""Test del parsing delle information table 13F (XML SEC, nessuna rete)."""

import pytest

from modules.institutional_holdings import edgar_13f

# Namespace e forma presi da una 13F reale (SEC thirteenf/informationtable).
SAMPLE_TABLE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable>
    <ns1:nameOfIssuer>ABBVIE INC</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>00287Y109</ns1:cusip>
    <ns1:value>571474</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>2271</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
    <ns1:investmentDiscretion>SOLE</ns1:investmentDiscretion>
  </ns1:infoTable>
  <ns1:infoTable>
    <ns1:nameOfIssuer>APPLE INC</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>037833100</ns1:cusip>
    <ns1:value>1540000</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>9000</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
    <ns1:investmentDiscretion>SHARED</ns1:investmentDiscretion>
  </ns1:infoTable>
  <ns1:infoTable>
    <ns1:nameOfIssuer>MICROSOFT CORP</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>594918104</ns1:cusip>
    <ns1:value>3000</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>15</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
    <ns1:putCall>CALL</ns1:putCall>
  </ns1:infoTable>
</ns1:informationTable>
"""


def test_parse_returns_one_row_per_infotable():
    rows = edgar_13f.parse_information_table(SAMPLE_TABLE.encode())
    assert len(rows) == 3


def test_parse_extracts_cusip_issuer_value_shares():
    row = edgar_13f.parse_information_table(SAMPLE_TABLE.encode())[0]
    assert row.cusip == "00287Y109"
    assert row.issuer_name == "ABBVIE INC"
    assert row.title_of_class == "COM"
    assert row.shares == 2271
    assert row.put_call is None


def test_value_is_kept_raw_no_unit_normalization():
    # La regola SEC è "migliaia di dollari" ma alcuni filer depositano in
    # dollari: il modulo NON converte, conserva il numero così com'è.
    row = edgar_13f.parse_information_table(SAMPLE_TABLE.encode())[0]
    assert row.value == 571474


def test_option_rows_are_flagged_with_putcall():
    rows = edgar_13f.parse_information_table(SAMPLE_TABLE.encode())
    assert rows[2].put_call == "CALL"
    assert rows[0].put_call is None


def test_parse_tolerates_missing_cusip_and_non_numeric_value():
    xml = SAMPLE_TABLE.replace("<ns1:cusip>00287Y109</ns1:cusip>", "")
    xml = xml.replace("<ns1:value>571474</ns1:value>", "<ns1:value>N/A</ns1:value>")
    row = edgar_13f.parse_information_table(xml.encode())[0]
    assert row.cusip == ""
    assert row.value is None


def test_is_information_table_detects_the_right_root():
    header = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">'
        b"<headerData/></edgarSubmission>"
    )
    assert edgar_13f._is_information_table(SAMPLE_TABLE.encode()) is True
    assert edgar_13f._is_information_table(header) is False


def test_is_information_table_handles_malformed_xml():
    assert edgar_13f._is_information_table(b"<not-xml") is False


def test_cik_list_is_split_when_efts_returns_a_string():
    assert edgar_13f._as_list("0001032814 0000877338") == ["0001032814", "0000877338"]
    assert edgar_13f._as_list(["0001032814"]) == ["0001032814"]
    assert edgar_13f._as_list(None) == []


def test_display_name_string_is_never_split_on_spaces():
    # Se EFTS restituisse una stringa, splittarla darebbe "BERKSHIRE".
    name = "BERKSHIRE HATHAWAY INC  (BRK-A, BRK-B)"
    assert edgar_13f._as_name_list(name) == [name]
    assert edgar_13f.clean_display_name("BERKSHIRE HATHAWAY INC  (CIK 0001067983)") == \
        "BERKSHIRE HATHAWAY INC"
    assert edgar_13f.clean_display_name("Thompson David Blair") == "Thompson David Blair"


def test_shares_type_is_parsed_to_separate_shares_from_bonds():
    rows = edgar_13f.parse_information_table(SAMPLE_TABLE.encode())
    assert rows[0].shares_type == "SH"


def test_principal_amount_rows_are_marked_as_prn():
    # Obbligazioni/fondi: sshPrnamtType = PRN → il modulo deve scartarli.
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <ns1:infoTable>
        <ns1:nameOfIssuer>US TREASURY BOND</ns1:nameOfIssuer>
        <ns1:titleOfClass>US T BILL</ns1:titleOfClass>
        <ns1:cusip>9128283P3</ns1:cusip>
        <ns1:value>1000</ns1:value>
        <ns1:shrsOrPrnAmt>
          <ns1:sshPrnamt>50000</ns1:sshPrnamt>
          <ns1:sshPrnamtType>PRN</ns1:sshPrnamtType>
        </ns1:shrsOrPrnAmt>
      </ns1:infoTable>
    </ns1:informationTable>"""
    row = edgar_13f.parse_information_table(xml.encode())[0]
    assert row.shares_type == "PRN"
    assert row.shares == 50000


def test_missing_shares_amount_does_not_raise():
    # shrsOrPrnAmt assente: prima sollevava AttributeError (iter su None).
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <ns1:infoTable>
        <ns1:nameOfIssuer>BROKEN FILER</ns1:nameOfIssuer>
        <ns1:cusip>123456789</ns1:cusip>
        <ns1:value>10</ns1:value>
      </ns1:infoTable>
    </ns1:informationTable>"""
    row = edgar_13f.parse_information_table(xml.encode())[0]
    assert row.shares is None
    assert row.shares_type is None


def test_invalid_xml_raises_typed_error_not_parseerror():
    # Serve al modulo per isolare il filing senza far fallire tutto il run.
    with pytest.raises(edgar_13f.SecEdgarError):
        edgar_13f.parse_information_table(b"<informationTable><infoTable>")