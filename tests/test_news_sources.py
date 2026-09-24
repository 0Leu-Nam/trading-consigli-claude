"""Test di parsing/normalizzazione delle sorgenti notizie (senza rete effettiva)."""

import pytest

from modules.news_sentiment import data_sources
from modules.news_sentiment.data_sources import NewsSourceError

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <guid>guid-abc</guid>
    <title>Apple stock is giving new CEO plenty to smile about</title>
    <link>https://www.thestreet.com/investing/stocks/apple</link>
    <pubDate>Thu, 24 Sep 2026 19:13:00 +0000</pubDate>
  </item>
  <item>
    <title>Missing guid item</title>
    <link>https://finance.yahoo.com/m/123/foo</link>
    <pubDate>Thu, 24 Sep 2026 18:00:00 +0000</pubDate>
  </item>
  <item>
    <guid>guid-no-title</guid>
    <link>https://x</link>
    <pubDate>Thu, 24 Sep 2026 17:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


# ── RSS Yahoo ─────────────────────────────────────────────────────────────────

def test_parse_rss_xml_basic():
    items = data_sources.parse_rss_xml(SAMPLE_RSS, "AAPL")
    assert len(items) == 2  # l'item senza titolo viene scartato
    assert items[0].uuid == "guid-abc"
    assert items[0].source == "yahoo_finance_rss"
    assert items[0].published_at.startswith("2026-09-24T")
    assert items[0].url == "https://www.thestreet.com/investing/stocks/apple"


def test_parse_rss_missing_guid_uses_url():
    items = data_sources.parse_rss_xml(SAMPLE_RSS, "AAPL")
    missing_guid = next(i for i in items if i.title.startswith("Missing guid"))
    assert missing_guid.uuid == data_sources.synthesize_uuid(missing_guid.url, missing_guid.title)
    assert missing_guid.title == "Missing guid item"


def test_parse_rss_malformed_raises():
    with pytest.raises(NewsSourceError):
        data_sources.parse_rss_xml("<rss><broken", "AAPL")


def test_fetch_yahoo_rss_http_error_is_isolated(monkeypatch):
    class _Resp:
        status_code = 404
        text = ""

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    with pytest.raises(NewsSourceError):
        data_sources.fetch_yahoo_rss("AAPL")


def test_fetch_yahoo_rss_network_error_is_isolated(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.Timeout("net")

    monkeypatch.setattr("requests.get", _boom)
    with pytest.raises(NewsSourceError):
        data_sources.fetch_yahoo_rss("AAPL")


def test_fetch_yahoo_rss_ok(monkeypatch):
    class _Resp:
        status_code = 200
        text = SAMPLE_RSS

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    items = data_sources.fetch_yahoo_rss("AAPL")
    assert len(items) == 2


# ── sintetizzare uuid ─────────────────────────────────────────────────────────

def test_synthesize_uuid_uses_url_preferred():
    a = data_sources.synthesize_uuid("https://x/one", "same title")
    b = data_sources.synthesize_uuid("https://x/one", "different title")
    c = data_sources.synthesize_uuid("https://x/two", "same title")
    assert a == b  # stesso url → stesso uuid, anche se il titolo cambia
    assert a != c  # url diversi → uuid diversi


def test_synthesize_uuid_fallback_title():
    a = data_sources.synthesize_uuid(None, "Only a title")
    b = data_sources.synthesize_uuid("", "Only a title")
    assert a == b


def test_synthesize_uuid_empty_raises():
    with pytest.raises(NewsSourceError):
        data_sources.synthesize_uuid(None, None)


# ── Marketaux ─────────────────────────────────────────────────────────────────

SAMPLE_MARKETAUX = """{
  "data": [
    {
      "uuid": "mk-1",
      "title": "Rally on strong earnings",
      "url": "https://marketaux.example/1",
      "published_at": "2026-09-24T12:30:00.000Z",
      "source": "Example Wire",
      "entities": [{"symbol": "AAPL", "entity_sentiment_score": 0.8}]
    },
    {
      "title": "No uuid article, url as key",
      "url": "https://marketaux.example/2",
      "published_at": "2026-09-24T13:00:00+02:00",
      "entities": [{"symbol": "MSFT", "entity_sentiment_score": -0.5}]
    },
    {
      "uuid": "mk-3",
      "title": "No sentiment article",
      "url": "https://marketaux.example/3",
      "published_at": "2026-09-24T14:00:00Z"
    }
  ]
}
"""


def test_parse_marketaux_basic():
    items = data_sources.parse_marketaux_json(SAMPLE_MARKETAUX, "AAPL")
    assert len(items) == 3
    assert items[0].uuid == "mk-1"
    assert items[0].source == "marketaux"
    assert items[0].sentiment_score == 0.8
    assert items[1].published_at.startswith("2026-09-24T")  # fuso normalizzato a UTC
    assert items[2].sentiment_score is None


def test_parse_marketaux_uuid_synthesized_from_url():
    items = data_sources.parse_marketaux_json(SAMPLE_MARKETAUX, "MSFT")
    second = items[1]
    assert second.uuid == data_sources.synthesize_uuid(second.url, second.title)


def test_parse_marketaux_empty_url_and_title_skipped():
    payload = '{"data": [{"uuid": "mk-x", "title": "", "published_at": "2026-09-24T12:00:00Z"}]}'
    assert data_sources.parse_marketaux_json(payload, "AAPL") == []


def test_parse_marketaux_bad_json_raises():
    with pytest.raises(NewsSourceError):
        data_sources.parse_marketaux_json("not json at all", "AAPL")


def test_parse_marketaux_non_dict_payload_raises():
    with pytest.raises(NewsSourceError):
        data_sources.parse_marketaux_json("[1,2,3]", "AAPL")


def test_fetch_marketaux_http_error_is_isolated(monkeypatch):
    class _Resp:
        status_code = 429
        text = ""

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    with pytest.raises(NewsSourceError):
        data_sources.fetch_marketaux("AAPL", "TOKEN")