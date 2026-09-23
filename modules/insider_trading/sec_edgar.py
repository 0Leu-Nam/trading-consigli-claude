"""Client SEC EDGAR per Form 4 (full-text search + parse XML ownership).

Politica fair-access SEC rispettata: User-Agent con contatto e max 10 req/sec
(noi usiamo una pausa di ~0.35s tra richieste). Nessuna chiave: la policy
richiede solo un User-Agent dichiarato, letto da env (SEC_EDGAR_USER_AGENT).

Efflusso per ogni filing scoperto via EFTS:
    1. ``index.json`` della cartella filing (trova il documento ownership .xml);
    2. scarica e parsa il documento Form 4 ownership.
"""

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "trading-consigli-claude admin@example.com"
EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
REQUEST_INTERVAL = 0.35  # secondi tra richieste consecutive (limite SEC 10/s)
MAX_RETRIES = 3


class SecEdgarError(RuntimeError):
    pass


@dataclass
class FilingDiscovery:
    """Una hit EFTS di Form 4, deduplicata per accession."""

    accession: str
    filing_date: str
    ciks: list[str]  # cik a 10 cifre (reporting owner, issuer, ...)


@dataclass
class InsiderTransaction:
    company_ticker: str
    company_name: str
    company_cik: str
    accession: str
    row_no: int
    filing_date: str
    transaction_date: Optional[str]
    insider_name: str
    insider_title: Optional[str]
    transaction_type: str  # codice SEC: P, S, M, A, C, ...
    shares: Optional[int]
    price_per_share: Optional[float]
    value_usd: Optional[int]
    holdings_after: Optional[int]
    is_open_market: bool
    url: str


def _get(url: str, user_agent: str, timeout: int = 30) -> requests.Response:
    """GET con retry/backoff e pausa fissa tra richieste (fair access SEC)."""
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(REQUEST_INTERVAL)
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (403, 429):
                # rate limit o blocco fair-access: ritenta con backoff più lungo
                wait = REQUEST_INTERVAL * (2**attempt)
                logger.warning("SEC %s su %s (tentativo %s), attesa %.1fs", resp.status_code, url, attempt, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise SecEdgarError(f"GET fallito per {url}: {exc}") from exc
            time.sleep(REQUEST_INTERVAL * (2**attempt))
    raise SecEdgarError(f"GET fallito per {url} dopo {MAX_RETRIES} tentativi")


def search_form4(user_agent: str, start_dt: str, end_dt: str, max_filings: int) -> list[FilingDiscovery]:
    """Cerca i Form 4 depositati in [start_dt, end_dt] via full-text search.

    Le hit EFTS sono DOCUMENTI dentro i filing (es. ownership.xml): si deduplica
    per accession. La paginazione usa il parametro ``from`` (100 per pagina).
    """
    discoveries: list[FilingDiscovery] = []
    seen: set[str] = set()
    offset = 0
    while len(discoveries) < max_filings:
        params = {
            "q": "",
            "forms": "4",
            "dateRange": "custom",
            "startdt": start_dt,
            "enddt": end_dt,
            "from": str(offset),
        }
        resp = _get(EFTS_SEARCH_URL + "?" + requests.compat.urlencode(params), user_agent)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SecEdgarError(f"Risposta EFTS non JSON: {resp.text[:200]}") from exc
        hits = (payload.get("hits") or {}).get("hits") or []
        if not hits:
            break
        for hit in hits:
            source = hit.get("_source") or {}
            adsh = source.get("adsh")
            if not adsh or adsh in seen:
                continue
            seen.add(adsh)
            raw_ciks = source.get("ciks") or []
            if isinstance(raw_ciks, str):
                raw_ciks = raw_ciks.split()
            ciks = [str(c).strip() for c in raw_ciks if str(c).strip()]
            discoveries.append(
                FilingDiscovery(accession=adsh, filing_date=source.get("file_date", ""), ciks=ciks)
            )
            if len(discoveries) >= max_filings:
                break
        offset += len(hits)
        if len(hits) < 100:
            break
    logger.info("EFTS: trovati %d filing Form 4 unici (%d pagine esaminate)", len(discoveries), offset // 100 + 1)
    return discoveries


def _candidate_issuer_cik(discovery: FilingDiscovery) -> list[str]:
    """Ordina i cik per tentativo: per un Form 4 il folder dati sta di solito
    sotto il reporting owner (primo cik). Si tenta poi gli altri in ordine."""
    return discovery.ciks[:3] or []


def fetch_ownership_xml_url(user_agent: str, discovery: FilingDiscovery) -> Optional[str]:
    """Trova l'URL del documento ownership .xml guardando index.json del filing.

    Il campo ``type`` di index.json non è affidabile (es. "text.gif" anche per
    XML): si seleziona per NOME, preferendo ownership.xml / primary_doc.xml /
    form4*.xml / wk-form4*.xml, altrimenti il primo .xml della cartella."""
    adsh_no_dash = discovery.accession.replace("-", "")
    for cik in _candidate_issuer_cik(discovery):
        index_url = f"{ARCHIVES_BASE}/{cik}/{adsh_no_dash}/index.json"
        try:
            resp = _get(index_url, user_agent)
        except SecEdgarError:
            continue
        try:
            payload = resp.json()
        except ValueError:
            continue
        files = (payload.get("directory") or {}).get("item") or []
        xml_names = [item.get("name", "") for item in files if str(item.get("name", "")).lower().endswith(".xml")]
        if not xml_names:
            continue
        preferred = [
            n for n in xml_names
            if "ownership" in n.lower() or "primary" in n.lower() or "form4" in n.lower()
        ]
        name = (preferred or xml_names)[0]
        return f"{ARCHIVES_BASE}/{cik}/{adsh_no_dash}/{name}"
    return None


def _localname(tag: str) -> str:
    """Rimuove il namespace dagli XML SEC (tag come '{ns}name')."""
    return tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, name: str) -> Optional[ET.Element]:
    for child in element.iter():
        if _localname(child.tag) == name:
            return child
    return None


def _findall(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _localname(child.tag) == name]


def _text(element: Optional[ET.Element]) -> Optional[str]:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_form4(xml_bytes: bytes, url: str, accession: str, row_offset: int = 0) -> tuple[str, str, str, list[InsiderTransaction]]:
    """Parsa un documento ownership (Form 4) e torna (ticker, company_name, cik, [transazioni]).

    Gestisce i namespace in modo agnostico. Ogni riga di nonDerivativeTable
    diventa una InsiderTransaction con row_no progressivo."""
    root = ET.fromstring(xml_bytes)

    issuer = _find(root, "issuer")
    ticker = _text(_find(issuer, "issuerTradingSymbol")) if issuer is not None else None
    issuer_name = _text(_find(issuer, "issuerName")) if issuer is not None else None
    issuer_cik = _text(_find(issuer, "issuerCik")) if issuer is not None else None

    reporting_owner = _find(root, "reportingOwner")
    owner_name = None
    owner_title = None
    if reporting_owner is not None:
        owner_name = _text(_find(_find(reporting_owner, "reportingOwnerId"), "rptOwnerName"))
        rel = _find(reporting_owner, "reportingOwnerRelationship")
        owner_title = _text(_find(rel, "officerTitle")) if rel is not None else None

    filing_date = _text(_find(root, "periodOfReport")) or ""

    transactions: list[InsiderTransaction] = []
    accepted_codes = {"P", "S", "M", "A", "C", "G", "D", "F", "X"}  # codici transazione comuni
    for row_no, tx in enumerate(_findall(root, "nonDerivativeTransaction"), start=row_offset):
        code_el = _text(_find(_find(tx, "transactionCoding"), "transactionCode"))
        if not code_el or code_el not in accepted_codes:
            continue
        transaction_code = code_el
        amounts = _find(tx, "transactionAmounts")
        shares = _to_int(_text(_find(_find(amounts, "transactionShares"), "value")))
        price = _to_float(_text(_find(_find(amounts, "transactionPricePerShare"), "value")))
        value = int(shares * price) if shares is not None and price else None
        holdings_el = _find(_find(tx, "postTransactionAmounts"), "sharesOwnedFollowingTransaction")
        holdings = _to_int(_text(_find(holdings_el, "value")))
        tx_date = _text(_find(_find(tx, "transactionDate"), "value"))

        transactions.append(
            InsiderTransaction(
                company_ticker=ticker or "",
                company_name=issuer_name or "",
                company_cik=issuer_cik or "",
                accession=accession,
                row_no=row_no,
                filing_date=filing_date,
                transaction_date=tx_date,
                insider_name=owner_name or "",
                insider_title=owner_title,
                transaction_type=transaction_code,
                shares=shares,
                price_per_share=price,
                value_usd=value,
                holdings_after=holdings,
                is_open_market=transaction_code in ("P", "S") and bool(price),
                url=url,
            )
        )
    return ticker or "", issuer_name or "", issuer_cik or "", transactions