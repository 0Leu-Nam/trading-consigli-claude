"""Sorgenti dati notizie per il modulo news_sentiment.

- ``yahoo_finance_rss``: feed RSS per-ticker senza chiave (XML standard).
- ``marketaux``: API REST con sentiment calcolato (chiave gratuita opzionale).

Isolamento: ogni funzione di fetch solleva :class:`NewsSourceError` per errori
tecnici localizzati (rete, HTTP non-200, rsposta non parsabile). Un singolo
item malformato non propaga mai errori: viene scartato silenziosamente. Il
modulo cattura il NewsSourceError per-ticker e continua.
"""

import hashlib
import logging
import uuid as uuid_mod
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class NewsSourceError(Exception):
    """Errore tecnico localizzato di una fonte notizie (rete, HTTP, formato)."""


@dataclass
class NewsItem:
    uuid: str
    symbol: str
    published_at: str          # ISO-8601 UTC
    source: str                # 'marketaux' | 'yahoo_finance_rss'
    title: str
    url: str | None = None
    sentiment_score: float | None = None


YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) trading-consigli/1.0"


def _yahoo_symbol(ticker: str) -> str:
    """Yahoo usa il trattino per le azioni di classe: BRK.B → BRK-B."""
    return ticker.replace(".", "-")


def synthesize_uuid(url: str | None, title: str | None) -> str:
    """UUID deterministico per gli articoli senza guid: base url, fallback titolo.

    Usare l'url (quando c'è) riduce i falsi non-duplicati rispetto al solo
    titolo. Concetto unico e stabile tra run → chiave di dedup in news_events.
    """
    base = (url or "").strip() or (title or "").strip()
    if not base:
        raise NewsSourceError("articolo senza url né titolo: impossibile deduplicare")
    return str(uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, base))


def _normalize_iso(value: str | None) -> str | None:
    """Normalizza un timestamp in ISO-8601 UTC (accetta Z o fuso offset)."""
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


# ── Yahoo Finance RSS ─────────────────────────────────────────────────────────

def _parse_rfc822(value: str | None) -> str | None:
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def parse_rss_xml(xml_text: str, symbol: str, limit: int = 15) -> list[NewsItem]:
    """Parsa un feed RSS Yahoo in list[NewsItem]. XML malformato → NewsSourceError.

    Item senza titolo/Data sono scartati; guid mancante → uuid syntetico.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NewsSourceError(f"Yahoo RSS {symbol}: XML non valido ({exc})") from exc

    items: list[NewsItem] = []
    for node in root.iter("item"):
        def _text(tag: str) -> str | None:
            child = node.find(tag)
            return child.text.strip() if child is not None and child.text else None

        title = _text("title")
        published = _parse_rfc822(_text("pubDate"))
        if not title or not published:
            continue
        guid = _text("guid")
        url = _text("link")
        uuid = guid or synthesize_uuid(url, title)
        items.append(
            NewsItem(
                uuid=uuid,
                symbol=symbol,
                published_at=published,
                source="yahoo_finance_rss",
                title=title,
                url=url,
                sentiment_score=None,
            )
        )
        if len(items) >= limit:
            break
    return items


def fetch_yahoo_rss(ticker: str, limit: int = 15, timeout: int = 30) -> list[NewsItem]:
    """Feed headline per-ticker di Yahoo Finance (gratis, senza chiave)."""
    import requests

    try:
        resp = requests.get(
            YAHOO_RSS_URL,
            params={"s": _yahoo_symbol(ticker), "region": "US", "lang": "en-US"},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise NewsSourceError(f"Yahoo RSS {ticker}: rete ({exc})") from exc
    if resp.status_code != 200:
        raise NewsSourceError(f"Yahoo RSS {ticker}: HTTP {resp.status_code}")
    return parse_rss_xml(resp.text, ticker, limit=limit)


# ── Marketaux API ─────────────────────────────────────────────────────────────

def parse_marketaux_json(text: str, symbol: str) -> list[NewsItem]:
    """Parsa la risposta JSON di /v1/news/all in list[NewsItem].

    Lo score di sentiment si prende dall'entità che combacia col simbolo
    richiesto (``entity_sentiment_score``), altrimenti dalla prima entità;
    se assente resta None (il modulo applicherà il fallback lessico).
    """
    import json

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise NewsSourceError(f"Marketaux {symbol}: JSON non valido ({exc})") from exc
    if not isinstance(payload, dict):
        raise NewsSourceError(f"Marketaux {symbol}: payload inatteso")

    items: list[NewsItem] = []
    for article in payload.get("data") or []:
        if not isinstance(article, dict):
            continue
        title = (article.get("title") or "").strip()
        published = _normalize_iso(article.get("published_at"))
        if not title or not published:
            continue
        url = (article.get("url") or "").strip() or None
        uuid = (article.get("uuid") or "").strip() or None
        uuid = uuid or synthesize_uuid(url, title)

        score = None
        entities = article.get("entities") or []
        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                sym = str(ent.get("symbol") or "").upper()
                val = ent.get("entity_sentiment_score")
                if isinstance(val, (int, float)):
                    if score is None or sym == symbol:
                        score = float(val)
        items.append(
            NewsItem(
                uuid=uuid,
                symbol=symbol,
                published_at=published,
                source="marketaux",
                title=title,
                url=url,
                sentiment_score=score,
            )
        )
    return items


def fetch_marketaux(ticker: str, api_token: str, limit: int = 3, timeout: int = 30) -> list[NewsItem]:
    """Notizie per-simbolo con sentiment di Marketaux (richiede il token)."""
    import requests

    try:
        resp = requests.get(
            MARKETAUX_URL,
            params={
                "symbols": ticker,
                "filter_entities": "true",
                "must_have_entities": "true",
                "language": "en",
                "limit": str(limit),
                "sort": "published_on",
                "sort_order": "desc",
                "api_token": api_token,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise NewsSourceError(f"Marketaux {ticker}: rete ({exc})") from exc
    if resp.status_code != 200:
        raise NewsSourceError(f"Marketaux {ticker}: HTTP {resp.status_code}")
    return parse_marketaux_json(resp.text, ticker)