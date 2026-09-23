"""Test di parsing/normalizzazione delle sorgenti prezzi (senza rete).

Non si scarica nulla: si verifica la trasformazione di dati già costruiti
(DataFrame sintetico yfinance, CSV sintetico Stooq) e il comportamento di
``fetch_history`` con un ``fetch_yf_history`` mockato.
"""

import pandas as pd
import pytest

from modules.price_screener import data_sources
from modules.price_screener.data_sources import parse_stooq_csv


def _build_multiindex_df():
    idx = pd.to_datetime(["2026-09-21", "2026-09-22"])
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["AAPL", "MSFT"]], names=["Price", "Ticker"])
    data = [
        [100.0, 50.0, 102.0, 52.0, 99.0, 49.0, 101.0, 51.0, 1_000_000, 2_000_000],
        [101.0, 51.5, 103.0, 52.5, 100.0, 50.5, 102.0, 52.0, 1_100_000, 1_900_000],
    ]
    return pd.DataFrame(data, index=idx, columns=cols)


def test_extract_and_normalize_multiindex_df():
    df = _build_multiindex_df()
    sub = data_sources._extract_ticker(df, "AAPL")
    bars = data_sources._df_to_bars(sub)
    assert len(bars) == 2
    assert bars[0].date == "2026-09-21"
    assert bars[0].close == 101.0
    assert bars[0].volume == 1_000_000
    assert bars[1].date == "2026-09-22"
    assert bars[1].open == 101.0


def test_extract_flat_single_ticker_df():
    idx = pd.to_datetime(["2026-09-21"])
    df = pd.DataFrame(
        {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5], "Volume": [1_000_000]},
        index=idx,
    )
    bars = data_sources._df_to_bars(data_sources._extract_ticker(df, "AAPL"))
    assert bars[0].date == "2026-09-21"
    assert bars[0].volume == 1_000_000


def test_df_to_bars_skips_nan_close_and_handles_nan_volume():
    idx = pd.to_datetime(["2026-09-21", "2026-09-22"])
    df = pd.DataFrame(
        {"Open": [100.0, None], "High": [101.0, None], "Low": [99.0, None], "Close": [float("nan"), 105.0], "Volume": [float("nan"), None]},
        index=idx,
    )
    bars = data_sources._df_to_bars(df)
    assert len(bars) == 1
    assert bars[0].close == 105.0
    assert bars[0].volume is None


def test_extract_ticker_returns_empty_for_missing_symbol():
    df = _build_multiindex_df()  # solo AAPL e MSFT
    sub = data_sources._extract_ticker(df, "DEAD")
    assert data_sources._df_to_bars(sub) == []


def test_yahoo_alias_replaces_dots():
    assert data_sources._yahoo_alias("BRK.B") == "BRK-B"
    assert data_sources._yahoo_alias("AAPL") == "AAPL"


def test_fetch_yf_history_maps_dotted_tickers_back_to_original(monkeypatch):
    # Yahoo risponde con "BRK-B" tra le colonne; il modulo deve tornare "BRK.B".
    idx = pd.to_datetime(["2026-09-22"])
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["BRK-B"]], names=["Price", "Ticker"])
    raw = pd.DataFrame([[100.0, 101.0, 99.0, 100.5, 1000]], index=idx, columns=cols)
    monkeypatch.setattr("yfinance.download", lambda *a, **k: raw)
    bars_map = data_sources.fetch_yf_history(["BRK.B"], days=30)
    assert len(bars_map["BRK.B"]) == 1
    assert bars_map["BRK.B"][0].close == 100.5


def test_parse_stooq_csv():
    text = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-09-18,100,101,99,100.5,1000000\n"
        "2026-09-21,100.5,102,100,101.5,1100000\n"
    )
    bars = parse_stooq_csv(text, "aapl.us")
    assert [b.date for b in bars] == ["2026-09-18", "2026-09-21"]  # ordinament cronologico
    assert bars[-1].close == 101.5
    assert bars[-1].volume == 1_100_000


def test_parse_stooq_csv_ignores_bad_rows():
    text = "Date,Open,High,Low,Close,Volume\n2026-09-18,,,,,\n2026-09-19,1,2,3,abc,10\n2026-09-22,10,12,9,11,100\n"
    bars = parse_stooq_csv(text, "x")
    assert len(bars) == 1
    assert bars[0].close == 11.0


def test_fetch_history_batch_uses_yf_and_lists_missing(monkeypatch):
    raw = _build_multiindex_df()
    monkeypatch.setattr(data_sources, "fetch_yf_history", lambda t, days=30: {"AAPL": data_sources._df_to_bars(data_sources._extract_ticker(raw, "AAPL"))})
    bars_map, missing = data_sources.fetch_history(["AAPL", "ZZZZ"], stooq_key=None)
    assert "AAPL" in bars_map and bars_map["AAPL"]
    assert "ZZZZ" not in bars_map
    assert missing == []


def test_fetch_history_falls_back_to_stooq_for_missing(monkeypatch):
    monkeypatch.setattr(data_sources, "fetch_yf_history", lambda t, days=30: {"DEAD": []})
    monkeypatch.setattr(data_sources, "fetch_stooq_history", lambda t, k, days=30: parse_stooq_csv("Date,Open,High,Low,Close,Volume\n2026-09-22,10,12,9,11,100\n", t))
    bars_map, missing = data_sources.fetch_history(["DEAD"], stooq_key="K")
    assert len(bars_map["DEAD"]) == 1
    assert missing == ["DEAD"]  # mancava da yfinance, recuperato su Stooq


def test_fetch_history_raises_when_primary_source_is_down(monkeypatch):
    def _always_fail(*a, **k):
        raise data_sources.PriceSourceError("429 Too Many Requests")

    monkeypatch.setattr(data_sources, "fetch_yf_history", _always_fail)
    with pytest.raises(data_sources.PriceSourceError):
        data_sources.fetch_history(["AAPL"])