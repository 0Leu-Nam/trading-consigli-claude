"""Client SEC EDGAR per Form 13F-HR (full-text search + parse information table).

Politica fair-access SEC rispettata: User-Agent con contatto e max 10 req/sec
(noi usiamo una pausa di ~0.35s tra richieste). Nessuna chiave: la policy
richiede solo un User-Agent dichiarato, letto da env (SEC_EDGAR_USER_AGENT).

Per ogni 13F-HR scoperto via EFTS:
    1. ``index.json`` della cartella filing per individuare il documento XML
       che contiene la ``<informationTable>`` (il NOME del file varia molto a
       seconda del software usato dal filer: ``InfoTable.xml``, ``spartaq2.xml``,
       ``wk13f*.xml``, ...): lo selezioniamo quindi per CONTENUTO, non per nome;
    2. scarica e parsa l'information table (schema SEC "thirteenf/informationtable").

Le hit EFTS espongono anche ``period_ending`` (fine trimestre) e
``display_names`` (nome del gestore): non serve aprire altri documenti del
filing per sapere di quale trimestre si tratta o chi è il filer.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_NAME_CIK_RE = re.compile(r"\s*\(CIK\s+\d+\)\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

DEFAULT_USER_AGENT = "trading-consigli-claude admin@example.com"
EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
FORM_13F = "13F-HR"
REQUEST_INTERVAL = 0.35  # secondi tra richieste consecutive (limite SEC 10/s)
MAX_RETRIES = 3
PAGE_SIZE = 100  # hit per pagina EFTS


class SecEdgarError(RuntimeError):
    pass


@dataclass
class Filing13F:
    """Una hit EFTS di 13F-HR, deduplicata per accession."""

    accession: str
    file_date: str            # data di deposito del filing
    period_ending: str        # fine del trimestre coperto dal 13F
    ciks: list[str]           # cik a 10 cifre del/i filer
    display_names: list[str]  # nome/i del filer


@dataclass
class HoldingRow:
    """Una riga di una information table 13F.

    ``value`` è il valore grezzo riportato dal filer: la regola SEC prevede
    migliaia di dollari, ma alcuni filer depositano in dollari → l'unità è
    filer-dipendente e non viene normalizzata (cfr. PROGRESS.md).
    """

    cusip: str
    issuer_name: str
    title_of_class: str
    value: Optional[int]
    shares: Optional[int]
    put_call: Optional[str]   # None = posizione su titoli, 'PUT'/'CALL' = opzione
    shares_type: Optional[str]  # 'SH' = azioni/quote, 'PRN' = principal amount (obbligazioni, fondi)


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


def _as_list(raw) -> list[str]:
    """Normalizza un campo EFTS che può arrivare come lista o stringa.

    Usata per i CIK (possibile elenco separato da spazi).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split()
    return [str(item).strip() for item in raw if str(item).strip()]


def _as_name_list(raw) -> list[str]:
    """Come _as_list ma per i nomi: NON spezza mai una stringa.

    EFTS restituisce `display_names` come lista di nomi completi, ognuno
    eventualmente con il suffisso "(CIK 0001067983)"; se arrivasse come
    stringa unica spezzarla sugli spazi darebbe "BERKSHIRE" come nome.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    return [str(item).strip() for item in raw if str(item).strip()]


def clean_display_name(name: str) -> str:
    """Rimuove il suffisso "(CIK nnnnnnnnnn)" che EFTS aggiunge ai nomi."""
    return _WS_RE.sub(" ", _NAME_CIK_RE.sub("", name or "")).strip()


def search_13f(
    user_agent: str,
    start_dt: str,
    end_dt: str,
    max_filings: int,
    ciks: list[str] | None = None,
) -> list[Filing13F]:
    """Cerca i 13F-HR depositati in [start_dt, end_dt] via full-text search.

    Le finestre sono quelle di DEPOSITO di un trimestre (q_end+1 .. q_end+45gg).
    Le hit EFTS sono documenti dentro i filing: si deduplica per accession.

    ``ciks`` filtra lato server (parametro EFTS ``ciks``, separati da virgola):
    serve a cercare gestori NOTI, perche' l'ordine di EFTS non e' quello del
    deposito che si cerca — i gestori richiesti possono stare anche in fondo
    alla coda (misurati: posizioni 1132/1857/2052 su ~4.000 accessions della
    finestra Q2 2026) e il fallback "primi N" prenderebbe altri filer.
    Con il filtro la richiesta e' una sola per finestra invece di N pagine.
    """
    discoveries: list[Filing13F] = []
    seen: set[str] = set()
    offset = 0
    while len(discoveries) < max_filings:
        params = {
            "q": "",
            "forms": FORM_13F,
            "dateRange": "custom",
            "startdt": start_dt,
            "enddt": end_dt,
            "from": str(offset),
        }
        if ciks:
            params["ciks"] = ",".join(ciks)
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
            discoveries.append(
                Filing13F(
                    accession=adsh,
                    file_date=source.get("file_date", ""),
                    period_ending=source.get("period_ending", ""),
                    ciks=_as_list(source.get("ciks")),
                    display_names=[
                        clean_display_name(n) for n in _as_name_list(source.get("display_names"))
                    ],
                )
            )
            if len(discoveries) >= max_filings:
                break
        offset += len(hits)
        if len(hits) < PAGE_SIZE:
            break
    logger.info(
        "EFTS: %d depositi 13F-HR unici in %s..%s (%d pagine esaminate, ciks=%s)",
        len(discoveries), start_dt, end_dt, offset // PAGE_SIZE + 1,
        ",".join(ciks) if ciks else "tutti",
    )
    return discoveries


def _xml_candidates(files: list[dict]) -> list[str]:
    """Nomina i file XML della cartella, dal più grande al più piccolo.

    La information table è di gran lunga il documento più grande del filing
    (il wrapper primary_doc.xml sta nell'ordine dei 2 KB). Ordinare per size
    porta subito al file giusto; ``primary_doc.xml`` resta comunque in coda
    come ultima risorsa (rari depositi con tabella nel documento primario).
    """
    xmls = [
        item for item in files
        if str(item.get("name", "")).lower().endswith(".xml")
    ]
    xmls.sort(key=lambda item: int(item.get("size") or 0), reverse=True)

    def _rank(item: dict) -> int:
        return 1 if str(item.get("name", "")).lower().startswith("primary_doc") else 0

    return [str(item["name"]) for item in sorted(xmls, key=_rank)] if xmls else []


def _is_information_table(xml_bytes: bytes) -> bool:
    """True se il documento è una information table 13F (root = informationTable)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False
    return _localname(root.tag) == "informationTable"


def discover_information_table(
    user_agent: str, accession: str, ciks: list[str]
) -> Optional[str]:
    """Torna l'URL del documento XML che contiene la information table 13F.

    Il tipo in index.json non è affidabile (spesso "text.gif" anche per gli
    XML): si seleziona per NOME (estensione .xml), poi si verifica il
    CONTENUTO scaricando i candidati dal più grande al più piccolo.
    """
    adsh_no_dash = accession.replace("-", "")
    for cik in ciks[:3]:
        index_url = f"{ARCHIVES_BASE}/{cik}/{adsh_no_dash}/index.json"
        try:
            resp = _get(index_url, user_agent)
            payload = resp.json()
        except (SecEdgarError, ValueError):
            continue
        files = (payload.get("directory") or {}).get("item") or []
        candidates = _xml_candidates(files)
        for name in candidates:
            url = f"{ARCHIVES_BASE}/{cik}/{adsh_no_dash}/{name}"
            try:
                content = _get(url, user_agent).content
            except SecEdgarError:
                continue
            if _is_information_table(content):
                return url
    return None


def _localname(tag: str) -> str:
    """Rimuove il namespace dagli XML SEC (tag come '{ns}name')."""
    return tag.rsplit("}", 1)[-1]


def _find(element: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if element is None:
        return None
    for child in element.iter():
        if _localname(child.tag) == name:
            return child
    return None


def _findall(element: Optional[ET.Element], name: str) -> list[ET.Element]:
    if element is None:
        return []
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


def parse_information_table(xml_bytes: bytes) -> list[HoldingRow]:
    """Parsa l'information table 13F e torna le righe titolo.

    Le posizioni su OPZIONI (``putCall`` valorizzato) sono mantenute: chi
    chiama decide se scartarle. Ogni riga è (cusip, emittente, valore grezzo,
    quantità, tipo quantità); il CUSIP è la chiave: non esiste mappa ufficiale
    CUSIP→ticker su EDGAR (vedi ``cusip_map.py``).

    ``shares_type`` distingue 'SH' (azioni) da 'PRN' (principal amount:
    obbligazioni, fondi, trust) — il modulo scarta i 'PRN' perché non sono
    posizioni azionarie confrontabili con i prezzi del progetto.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise SecEdgarError(f"XML information table non valido: {exc}") from exc
    rows: list[HoldingRow] = []
    for row in _findall(root, "infoTable"):
        amount = _find(row, "shrsOrPrnAmt")
        rows.append(
            HoldingRow(
                cusip=_text(_find(row, "cusip")) or "",
                issuer_name=_text(_find(row, "nameOfIssuer")) or "",
                title_of_class=_text(_find(row, "titleOfClass")) or "",
                value=_to_int(_text(_find(row, "value"))),
                shares=_to_int(_text(_find(amount, "sshPrnamt"))),
                put_call=_text(_find(row, "putCall")),
                shares_type=(_text(_find(amount, "sshPrnamtType")) or "").upper() or None,
            )
        )
    return rows