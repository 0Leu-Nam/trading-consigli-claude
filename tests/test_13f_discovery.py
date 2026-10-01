"""Test della scoperta del documento information table 13F.

Il nome del file con la tabella varia per software del filer (InfoTable.xml,
spartawq2.xml, wk13f*.xml, ...): la selezione è quindi per CONTENUTO.
"""

from modules.institutional_holdings import edgar_13f

# Payload index.json reale (tipo sempre "text.gif", anche per gli XML).
INDEX_PAYLOAD = {
    "directory": {
        "item": [
            {"name": "0002150492-26-000003-index.html", "type": "text.gif", "size": 900},
            {"name": "primary_doc.xml", "type": "text.gif", "size": 1974},
            {"name": "spartawq2.xml", "type": "text.gif", "size": 112530},
        ]
    }
}

TABLE_XML = b"<informationTable><infoTable><cusip>1</cusip></infoTable></informationTable>"
HEADER_XML = b'<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler"><headerData/></edgarSubmission>'


class _Resp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content
        self.text = content.decode(errors="ignore")

    def json(self):
        if self._payload is None:
            raise ValueError("non json")
        return self._payload


def test_candidates_are_ordered_by_size_with_primary_doc_last():
    files = INDEX_PAYLOAD["directory"]["item"]
    assert edgar_13f._xml_candidates(files) == ["spartawq2.xml", "primary_doc.xml"]


def test_candidates_ignore_non_xml_files():
    files = [
        {"name": "0002150492-26-000003.txt", "size": 5000},
        {"name": "InfoTable.xml", "size": 10},
    ]
    assert edgar_13f._xml_candidates(files) == ["InfoTable.xml"]


def test_discover_picks_the_document_that_really_is_the_table(monkeypatch):
    fetched = []

    def fake_get(url, *a, **k):
        fetched.append(url)
        if url.endswith("index.json"):
            return _Resp(payload=INDEX_PAYLOAD)
        if url.endswith("primary_doc.xml"):
            return _Resp(content=HEADER_XML)
        return _Resp(content=TABLE_XML)

    monkeypatch.setattr(edgar_13f, "_get", fake_get)
    url = edgar_13f.discover_information_table("UA", "0002150492-26-000003", ["0002150492"])
    assert url is not None and url.endswith("/spartawq2.xml")
    # L'ordinamento per size evita di scaricare il wrapper primary_doc.xml.
    assert not any(f.endswith("primary_doc.xml") for f in fetched)


def test_discover_falls_back_to_primary_doc_when_it_is_the_only_xml(monkeypatch):
    payload = {"directory": {"item": [{"name": "primary_doc.xml", "size": 50000}]}}

    def fake_get(url, *a, **k):
        if url.endswith("index.json"):
            return _Resp(payload=payload)
        return _Resp(content=TABLE_XML)

    monkeypatch.setattr(edgar_13f, "_get", fake_get)
    url = edgar_13f.discover_information_table("UA", "0000000001-26-000001", ["0000000001"])
    assert url is not None and url.endswith("primary_doc.xml")


def test_discover_returns_none_when_no_information_table_exists(monkeypatch):
    def fake_get(url, *a, **k):
        if url.endswith("index.json"):
            return _Resp(payload=INDEX_PAYLOAD)
        return _Resp(content=HEADER_XML)

    monkeypatch.setattr(edgar_13f, "_get", fake_get)
    assert edgar_13f.discover_information_table("UA", "0002150492-26-000003", ["0002150492"]) is None