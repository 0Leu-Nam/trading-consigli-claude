"""Test della mappatura CUSIP → ticker costruita dai dati EDGAR (rete mockata)."""

import json

import pytest

from core import db
from modules.institutional_holdings import cusip_map

TICKER_MAP_PAYLOAD = {
    "0": {"cik_str": 1551152, "ticker": "ABBV", "title": "AbbVie Inc."},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "2": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    # EDGAR elenca anche le prefredenziali con lo stesso titolo normalizzato
    "3": {"cik_str": 37996, "ticker": "F-PD", "title": "FORD MOTOR CO"},
    "4": {"cik_str": 37996, "ticker": "F", "title": "Ford Motor Co."},
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "app.db"
    db.init_schema(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def _mock_ticker_map(monkeypatch, payload=None, allow=True):
    """Mocka il download di company_tickers.json e azzera la cache di processo."""
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)
        if not allow:
            raise AssertionError("mappa ticker richiesta quando non deve")
        return _Resp(payload if payload is not None else TICKER_MAP_PAYLOAD)

    monkeypatch.setattr(cusip_map, "_get", fake_get)
    monkeypatch.setattr(cusip_map, "_MAP_TTL_SECONDS", 0)  # forza il reload
    return calls


def test_normalize_name_ignores_case_spaces_and_punctuation():
    assert cusip_map.normalize_name("AbbVie Inc.") == "ABBVIEINC"
    assert cusip_map.normalize_name("ABBVIE  INC") == "ABBVIEINC"
    assert cusip_map.normalize_name("MICROSOFT CORP") == "MICROSOFTCORP"
    assert cusip_map.normalize_name(None) == ""


def test_core_key_drops_legal_tokens_and_sorts():
    assert cusip_map.core_key("DISNEY WALT CO") == cusip_map.core_key("Walt Disney Co")
    assert cusip_map.core_key("CHEVRON CORPORATION") == ("CHEVRON",)
    assert cusip_map.core_key("BANK AMERICA CORP") == cusip_map.core_key("BANK OF AMERICA CORP /DE/")
    assert cusip_map.core_key("BERKSHIRE HATHAWAY INC DEL") == ("BERKSHIRE", "HATHAWAY")
    assert cusip_map.core_key("APPLE INC CLASS A") == ("APPLE",)


def test_build_index_indexes_exact_and_core_levels():
    index = cusip_map.build_index(TICKER_MAP_PAYLOAD)
    assert index["exact"]["ABBVIEINC"][0]["ticker"] == "ABBV"
    assert index["exact"]["ABBVIEINC"][0]["cik"] == "0001551152"  # CIK a 10 cifre
    assert {e["ticker"] for e in index["core"][("FORD", "MOTOR")]} == {"F", "F-PD"}
    assert "AAPL" in [e["ticker"] for e in index["exact"]["APPLEINC"]]


def test_build_index_skips_malformed_entries():
    index = cusip_map.build_index({"a": {"ticker": "OK", "title": "OK CORP"}, "b": {"foo": 1}})
    assert [e["ticker"] for e in index["exact"]["OKCORP"]] == ["OK"]


def test_pick_requires_an_unambiguous_candidate():
    unambiguous = [{"ticker": "ABBV", "cik": "1", "title": "AbbVie Inc."}]
    assert cusip_map.pick(unambiguous)["ticker"] == "ABBV"

    # Stesso titolo per classe ordinaria e prefredenziale: si preferisce la ordinaria
    with_pref = [
        {"ticker": "F-PD", "cik": "1", "title": "FORD MOTOR CO"},
        {"ticker": "F", "cik": "2", "title": "Ford Motor Co."},
    ]
    assert cusip_map.pick(with_pref)["ticker"] == "F"

    # Due ticker entrambi "puliti" → ambigui: meglio non risolvere
    ambiguous = [
        {"ticker": "GOOG", "cik": "1", "title": "Alphabet Inc."},
        {"ticker": "GOOGL", "cik": "2", "title": "Alphabet Inc."},
    ]
    assert cusip_map.pick(ambiguous) is None
    assert cusip_map.pick([]) is None
    assert cusip_map.pick(None) is None


def test_resolve_matches_name_ignoring_case_and_punctuation(conn, monkeypatch):
    _mock_ticker_map(monkeypatch)
    ticker, company_id = cusip_map.resolve("UA", conn, "00287Y109", "ABBVIE  INC")
    conn.commit()
    assert ticker == "ABBV"
    assert company_id is not None
    company = conn.execute("SELECT ticker, name, cik FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert company["ticker"] == "ABBV"
    assert company["cik"] == "0001551152"
    cached = conn.execute("SELECT ticker, source, resolved_at FROM cusip_lookup WHERE cusip = '00287Y109'").fetchone()
    assert cached["ticker"] == "ABBV"
    assert cached["source"] == "edgar_name"
    assert cached["resolved_at"] is not None


def test_resolve_tracks_unresolved_cusip_and_retries_next_run(conn, monkeypatch):
    calls = _mock_ticker_map(monkeypatch)
    assert cusip_map.resolve("UA", conn, "XXXXXX999", "ACME WIDGETS LTD") == (None, None)
    conn.commit()
    row = conn.execute("SELECT company_id, ticker, resolved_at FROM cusip_lookup WHERE cusip = 'XXXXXX999'").fetchone()
    assert row["company_id"] is None and row["ticker"] is None
    assert row["resolved_at"] is None  # non risolto → verrà ritentato

    # Secondo run: il CUSIP viene ri-risolto (nuova lettura della mappa).
    cusip_map.resolve("UA", conn, "XXXXXX999", "ACME WIDGETS LTD")
    assert len(calls) == 2


def test_resolve_uses_fresh_cache_without_refetching_the_map(conn, monkeypatch):
    _mock_ticker_map(monkeypatch)
    cusip_map.resolve("UA", conn, "037833100", "APPLE INC")
    conn.commit()

    # Dopo la prima risoluzione la mappa non deve più essere richiesta.
    _mock_ticker_map(monkeypatch, allow=False)
    ticker, company_id = cusip_map.resolve("UA", conn, "037833100", "APPLE INC")
    assert ticker == "AAPL"
    assert company_id is not None


def test_resolve_refreshes_resolution_after_ttl_expiry(conn, monkeypatch):
    _mock_ticker_map(monkeypatch)
    cusip_map.resolve("UA", conn, "037833100", "APPLE INC")
    conn.execute("UPDATE cusip_lookup SET resolved_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()

    _mock_ticker_map(monkeypatch)
    ticker, _ = cusip_map.resolve("UA", conn, "037833100", "APPLE INC", ttl_days=60)
    conn.commit()
    assert ticker == "AAPL"
    row = conn.execute("SELECT resolved_at FROM cusip_lookup WHERE cusip = '037833100'").fetchone()
    assert not row["resolved_at"].startswith("2020")


def test_resolution_survives_a_conflicting_cik(conn, monkeypatch):
    # companies.cik ha un indice unico: se il CIK è già di un altro ticker
    # l'upsert non deve far esplodere il modulo.
    db.upsert_company(conn, "OLDCO", cik="0001551152")
    conn.commit()
    _mock_ticker_map(monkeypatch)
    ticker, company_id = cusip_map.resolve("UA", conn, "00287Y109", "ABBVIE INC")
    conn.commit()
    assert ticker == "ABBV"
    assert company_id is not None


CORE_MAP_PAYLOAD = {
    "0": {"cik_str": 93410, "ticker": "CVX", "title": "CHEVRON CORP"},
    "1": {"cik_str": 70858, "ticker": "BAC", "title": "BANK OF AMERICA CORP /DE/"},
    "2": {"cik_str": 1744489, "ticker": "DIS", "title": "Walt Disney Co"},
}


def test_core_tier_resolves_names_that_differ_only_by_legal_suffixes(conn, monkeypatch):
    _mock_ticker_map(monkeypatch, payload=CORE_MAP_PAYLOAD)
    cases = {
        "CHEVRON CORPORATION": "CVX",
        "BANK AMERICA CORP": "BAC",
        "DISNEY WALT CO": "DIS",
    }
    for cusip, (name, expected) in enumerate(cases.items()):
        ticker, company_id = cusip_map.resolve("UA", conn, f"CUSIP{cusip}", name)
        conn.commit()
        assert ticker == expected
        assert company_id is not None
    source = conn.execute(
        "SELECT source FROM cusip_lookup WHERE cusip = 'CUSIP0'"
    ).fetchone()["source"]
    assert source == "edgar_core"


def test_core_tier_can_be_disabled(conn, monkeypatch):
    _mock_ticker_map(monkeypatch, payload=CORE_MAP_PAYLOAD)
    ticker, company_id = cusip_map.resolve(
        "UA", conn, "CUSIPX", "CHEVRON CORPORATION", core_matching=False
    )
    conn.commit()
    assert ticker is None and company_id is None


def test_ambiguous_core_match_is_left_unresolved(conn, monkeypatch):
    payload = {
        "0": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
        "1": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    }
    _mock_ticker_map(monkeypatch, payload=payload)
    ticker, company_id = cusip_map.resolve("UA", conn, "CUSIPY", "ALPHABET INC")
    conn.commit()
    assert ticker is None and company_id is None
    row = conn.execute(
        "SELECT company_id, resolved_at FROM cusip_lookup WHERE cusip = 'CUSIPY'"
    ).fetchone()
    assert row["company_id"] is None and row["resolved_at"] is None


# --- Gate sull'evidenza del match "core" (regressione sugli ETF) ---------------
# "GLOBAL X FDS" riduceva a ("GLOBAL",) e veniva attribuito a "S&P Global Inc."
# (SPGI): 7 CUSIP di ETF finivano su un titolo dell'indice.
ETF_PAYLOAD = {
    "0": {"cik_str": 64040, "ticker": "SPGI", "title": "S&P Global Inc."},
    "1": {"cik_str": 1083301, "ticker": "WT", "title": "WisdomTree, Inc."},
    "2": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
}


def test_core_key_collapses_etf_names_to_one_generic_token():
    # Documenta il difetto: il nome del veicolo collassa a un aggettivo.
    assert cusip_map.core_key("GLOBAL X FDS") == ("GLOBAL",)
    assert cusip_map.core_key("S&P Global Inc.") == ("GLOBAL",)
    assert cusip_map.core_key("WISDOMTREE TR") == ("WISDOMTREE",)


def test_core_match_is_reliable_requires_two_tokens_or_legal_discards():
    # Scarti solo legali → affidabile anche con un solo token.
    assert cusip_map.core_match_is_reliable("CHEVRON CORPORATION") is True
    assert cusip_map.core_match_is_reliable("NVIDIA CORP") is True
    # Scarti di rumore/contenitori → non affidabile.
    assert cusip_map.core_match_is_reliable("GLOBAL X FDS") is False
    assert cusip_map.core_match_is_reliable("WISDOMTREE TR") is False
    # Con 2+ token la perdita di rumore è tollerabile.
    assert cusip_map.core_match_is_reliable("ISHARES GOLD TR") is True
    assert cusip_map.core_match_is_reliable("BANK OF AMERICA CORP /DE/") is True
    assert cusip_map.core_match_is_reliable(None) is False


def test_etf_vehicle_names_are_not_attributed_to_a_company(conn, monkeypatch):
    _mock_ticker_map(monkeypatch, payload=ETF_PAYLOAD)
    assert cusip_map.resolve("UA", conn, "37954Y293", "GLOBAL X FDS") == (None, None)
    assert cusip_map.resolve("UA", conn, "97408W104", "WISDOMTREE TR") == (None, None)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"] == 0
    unresolved = conn.execute(
        "SELECT COUNT(*) AS n FROM cusip_lookup WHERE resolved_at IS NULL"
    ).fetchone()["n"]
    assert unresolved == 2  # tracciati per il retry


def test_evidence_gate_does_not_over_block_real_matches(conn, monkeypatch):
    _mock_ticker_map(monkeypatch, payload=ETF_PAYLOAD)
    ticker, company_id = cusip_map.resolve("UA", conn, "67066G104", "NVIDIA CORPORATION")
    conn.commit()
    assert ticker == "NVDA"
    assert company_id is not None
    assert conn.execute(
        "SELECT source FROM cusip_lookup WHERE cusip = '67066G104'"
    ).fetchone()["source"] == "edgar_core"