"""Mappatura CUSIP → ticker costruita dai dati EDGAR (nessuna chiave, zero costi).

Su EDGAR NON esiste una mappa ufficiale e gratuita CUSIP→ticker: il file
``company_tickers.json`` contiene {ticker, cik_str, title} e nessun CUSIP, e
l'information table 13F riporta solo ``nameOfIssuer`` + ``cusip``.

Strategia: match per NOME normalizzato tra ``nameOfIssuer`` della 13F e
``title`` della mappa EDGAR. Esempio verificato: 13F "ABBVIE INC" ↔ EDGAR
"AbbVie Inc." → ticker ABBV, CIK 1551152. Funziona bene sui nomi esatti
(società quotate del nostro universe, ETF con ticker): i CUSIP non risolti
(obbligazioni, depositi privati, società senza ticker) NON vengono inseriti in
institutional_holdings ma restano tracciati in ``cusip_lookup`` con
``resolved_at = NULL``, quindi ritentati automaticamente ai run successivi.

Limiti noti (misurati su dati reali, vedi PROGRESS.md):
- Le 13F riportano la CLASSE in ``titleOfClass`` ma ``company_tickers.json``
  non la riporta: chi ha piu' ticker uguali (Alphabet GOOG/GOOGL, AT&T
  T/T-PA, JPMORGAN CHASE & CO con 9 ticker) resta non risolto per scelta.
- Gli ETF e i fondi sono quasi assenti dalla mappa (324 titoli su 10.431 hanno
  parole da fondo, zero voci per QQQ/DIMENSIONAL/GLOBAL X): le loro posizioni
  restano non risolte e non sono un problema per lo screening azionario.
- Con le sole forme legali si recupera circa il 30% dei CUSIP; le abbreviazioni
  in stile 13F ("APPLIED MATLS", "CISCO SYS", "EXXON MOBIL") restano aperte.

Enrichment opzionale futuro (fuori perimetro v1): API OpenFIGI con chiave in
.env per risolvere i CUSIP il cui nome non matcha.
"""

import logging
import re
import time
from typing import Optional

from core import db
from modules.institutional_holdings.edgar_13f import SecEdgarError, _get

logger = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_MAP_TTL_SECONDS = 24 * 3600  # la mappa EDGAR cambia raramente: 1 giorno basta
_map_cache: dict = {}  # {"index": {...}, "fetched_at": float}

# Token che le due fonti scrivono in modo diverso. Sono di due specie diverse e
# vanno trattati con pesi diversi, altrimenti il confronto produce match
# SBAGLIATI invece di mancanti (vedi core_evidence).
#
# 1) Forme legali societarie: scartarle è innocuo, sono la parte "non essenziale"
#    del nome. "CHEVRON CORPORATION" (13F) puo' cosi' trovare "CHEVRON CORP".
_LEGAL_FORM_TOKENS = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LP", "LLP", "LLC", "PLC", "LTD", "LIMITED", "SA", "NV", "AG", "GMBH",
    "PFD", "PRF", "CLASS",
}

# 2) Contenitori di fondi e connettivi: scartarlipuo' cancellare il segnale.
#    Caso reale: "GLOBAL X FDS" ridotto a ("GLOBAL",) che collideva con
#    "S&P Global Inc." (anch'esso ("GLOBAL",)) e attribuiva 7 CUSIP di ETF a
#    SPGI, un titolo dell'indice. "WISDOMTREE TR" finiva su WT.
_NOISE_TOKENS = {
    "TR", "TRUST", "ETF", "ETN", "FUND", "FDS", "FD", "SER", "SERIES",
    "SERVICE", "SERVICES", "GROUP", "THE", "OF", "AND", "ON", "FOR",
    "A", "I", "DE", "DEL",
}

# Unione: insieme a core_key() è il dizionario effettivo del confronto "core".
_LEGAL_TOKENS = _LEGAL_FORM_TOKENS | _NOISE_TOKENS


def normalize_name(name: Optional[str]) -> str:
    """Normalizza un nome società per il match esatto: solo A-Z0-9, maiuscolo.

    "AbbVie Inc." → ABBVIEINC ; "ABBVIE  INC" → ABBVIEINC : i due lati coincidono
    indipendentemente da punteggiatura, spazi e maiuscole.
    """
    if not name:
        return ""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def core_evidence(name: Optional[str]) -> tuple[tuple[str, ...], bool]:
    """Chiave "core" + quanto di affidabile ha il confronto.

    Ritorna (token_utili_ordinati, scarti_tutti_legali). Il secondo valore
    serve a distinguere due casi che la sola chiave non distingue:

    - scarti solo forme legali  ("NVIDIA CORPORATION" -> NVIDIA): il confronto
      resta valido anche se resta un solo token;
    - scarti rumore/contenitori ("GLOBAL X FDS" -> GLOBAL): qui la chiave e'
      un aggettivo generico e il confronto NON e' attendibile, altrimenti
      collide con altri titoli che condividono la stessa parola.

    L'ordine dei token viene perso di proposito: il contenuto deve coincidere.
    """
    if not name:
        return (), False
    kept: list[str] = []
    discarded: list[str] = []
    for token in re.findall(r"[A-Z0-9]+", name.upper()):
        if token in _LEGAL_TOKENS or len(token) <= 1:
            discarded.append(token)
        else:
            kept.append(token)
    return tuple(sorted(kept)), all(t in _LEGAL_FORM_TOKENS for t in discarded)


def core_key(name: Optional[str]) -> tuple[str, ...]:
    """Chiave "core": token utili del nome, senza suffissi legali, in ordine."""
    return core_evidence(name)[0]


def core_match_is_reliable(name: Optional[str]) -> bool:
    """Un match "core" e' attendibile solo con >=2 token o con scarti tutti legali.

    Applicata a ciascun lato del confronto (nome 13F e titolo EDGAR). Con un
    solo token generico il confronto e' rumore: meglio lasciare il CUSIP non
    risolto (e tracciato per il retry) che attribuirlo al titolo sbagliato.
    """
    key, strong = core_evidence(name)
    return bool(key) and (len(key) >= 2 or strong)


def _entry(title: str, item: dict) -> dict:
    raw_cik = item.get("cik_str")
    cik = None
    if raw_cik is not None:
        try:
            cik = str(int(raw_cik)).zfill(10)
        except (TypeError, ValueError):
            cik = str(raw_cik).zfill(10)
    return {
        "ticker": str(item.get("ticker") or ""),
        "cik": cik,
        "title": title,
        # attendibilità del confronto "core" su questo titolo EDGAR: False se la
        # sua chiave si riduce a una parola generica scartando solo rumore
        # (es. "S&P Global Inc." -> GLOBAL). Serve a non attribuire un CUSIP
        # di ETF a un titolo estraneo.
        "core_ok": core_match_is_reliable(title),
    }


def _add(index: dict, key, entry: dict) -> None:
    if not key:
        return
    bucket = index.setdefault(key, [])
    if not any(other["ticker"] == entry["ticker"] for other in bucket):
        bucket.append(entry)


def build_index(payload: dict) -> dict[str, dict]:
    """Indizza company_tickers.json su due livelli: esatto e "core".

    Ogni livello tiene una LISTA di candidati: EDGAR contiene anche azioni di
    classe diversa con lo stesso titolo normalizzato (es. "FORD MOTOR CO" per
    F e F-PD), quindi la scelta finale deve essere univoca per non attribuire
    una posizione al titolo sbagliato.
    """
    index: dict[str, list[dict]] = {"exact": {}, "core": {}}
    values = payload.values() if isinstance(payload, dict) else payload
    for item in values:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        if not title:
            continue
        entry = _entry(title, item)
        if not entry["ticker"]:
            continue
        _add(index["exact"], normalize_name(title), entry)
        _add(index["core"], core_key(title), entry)
    return index


def pick(candidates: Optional[list[dict]]) -> Optional[dict]:
    """Sceglie il candidato solo se è univoco.

    Se due ticker condividono il titolo (classe ordinaria e prefredenziale)
    si preferisce quello senza suffisso di classe ("F" invece di "F-PD");
    se la scelta resta ambigua si rinuncia (il CUSIP resta non risolto e
    viene ritentato ai run successivi).
    """
    if not candidates:
        return None
    tickers = {c["ticker"] for c in candidates}
    if len(tickers) == 1:
        return candidates[0]
    plain = [c for c in candidates if "-" not in c["ticker"] and "." not in c["ticker"]]
    plain_tickers = {c["ticker"] for c in plain}
    if len(plain_tickers) == 1:
        return plain[0]
    return None


def load_ticker_map(user_agent: str) -> dict[str, dict]:
    """Scarica (una volta al giorno) la mappa ticker EDGAR e la tiene in memoria."""
    now = time.monotonic()
    cached = _map_cache.get("index")
    if cached is not None and now - _map_cache.get("fetched_at", 0) < _MAP_TTL_SECONDS:
        return cached
    resp = _get(TICKER_MAP_URL, user_agent, timeout=60)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SecEdgarError(f"company_tickers.json non JSON: {resp.text[:200]}") from exc
    index = build_index(payload)
    _map_cache["index"] = index
    _map_cache["fetched_at"] = now
    logger.info("Mappa ticker EDGAR: %d titoli indicizzati", len(index))
    return index


def _cache_row(conn, cusip: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT company_id, ticker, resolved_at FROM cusip_lookup WHERE cusip = ?",
        (cusip,),
    ).fetchone()
    return dict(row) if row else None


def _save_cache(conn, cusip: str, issuer_name: str, *, company_id=None, ticker=None,
                source: Optional[str] = None) -> None:
    """Scrive la cache del CUSIP senza degradare una risoluzione già valida."""
    conn.execute(
        """
        INSERT INTO cusip_lookup (cusip, company_id, ticker, issuer_name, source, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (cusip) DO UPDATE SET
            company_id  = COALESCE(excluded.company_id,  cusip_lookup.company_id),
            ticker      = COALESCE(excluded.ticker,      cusip_lookup.ticker),
            issuer_name = COALESCE(excluded.issuer_name, cusip_lookup.issuer_name),
            source      = COALESCE(excluded.source,      cusip_lookup.source),
            resolved_at = COALESCE(excluded.resolved_at, cusip_lookup.resolved_at)
        """,
        (cusip, company_id, ticker, issuer_name, source, db.utcnow_iso() if company_id else None),
    )


def _company_from_map(conn, ticker: str, issuer_name: str, cik: str) -> Optional[int]:
    """Crea/aggiorna la company dal ticker EDGAR, tollerando CIK già assegnati."""
    try:
        return db.upsert_company(conn, ticker, name=issuer_name or None, cik=cik or None)
    except Exception as exc:  # indice unico su companies.cik può confligere
        logger.warning("upsert_company(%s, cik=%s) fallito: %s", ticker, cik, exc)
        return db.upsert_company(conn, ticker, name=issuer_name or None)


def resolve(
    user_agent: str,
    conn,
    cusip: str,
    issuer_name: str,
    *,
    ttl_days: int = 60,
    core_matching: bool = True,
) -> tuple[Optional[str], Optional[int]]:
    """Risolve un CUSIP in (ticker, company_id); (None, None) se non risolvibile.

    Ordine: cache ``cusip_lookup`` fresca → match esatto sulla mappa EDGAR →
    match "core" (stessa mappa, nomi che si differenziano solo per suffissi
    legali) se ``core_matching``. In entrambi i casi serve un candidato
    univoco, e il match "core" richiede anche che l'evidenza sia sufficiente
    (vedi ``core_match_is_reliable``). I CUSIP non risolti vengono comunque
    tracciati per il retry.
    """
    row = _cache_row(conn, cusip)
    if row and row.get("resolved_at") and row.get("company_id"):
        age_days = (time.time() - _iso_to_epoch(row["resolved_at"])) / 86400.0
        if age_days <= ttl_days:
            return row.get("ticker"), row.get("company_id")

    index = load_ticker_map(user_agent)
    entry = pick(index["exact"].get(normalize_name(issuer_name)))
    source = "edgar_name"
    if entry is None and core_matching:
        # Match "core": serve che il confronto sia ATTENDIBILE su entrambi i
        # lati (nome 13F e titolo EDGAR), altrimenti il CUSIP resta non
        # risolto: un nome che collassa a una parola generica ("GLOBAL X FDS"
        # -> GLOBAL) matchkerebbe titoli estranei.
        candidates = index["core"].get(core_key(issuer_name)) or []
        if candidates and all(c.get("core_ok") for c in candidates) \
                and core_match_is_reliable(issuer_name):
            entry = pick(candidates)
            source = "edgar_core"
    if not entry:
        _save_cache(conn, cusip, issuer_name)
        return None, None
    company_id = _company_from_map(conn, entry["ticker"], issuer_name, entry["cik"])
    _save_cache(
        conn, cusip, issuer_name, company_id=company_id,
        ticker=entry["ticker"], source=source,
    )
    return entry["ticker"], company_id


def _iso_to_epoch(iso_text: str) -> float:
    from datetime import datetime, timezone

    try:
        return datetime.fromisoformat(iso_text).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0