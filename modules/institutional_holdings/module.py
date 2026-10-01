"""Modulo institutional_holdings: 13F-HR trimestrali SEC EDGAR → institutional_holdings.

Solo raccolta/segnalazione, nessun ordine. Idempotente: UNIQUE(filing_quarter,
filer_cik, cusip) + INSERT OR IGNORE → un secondo run non duplica.

Ciclo trimestrale: i 13F si depositano entro ~45 giorni dalla fine del
trimestre, quindi qui si ragiona per TRIMESTRE CHIUSI (con lag >= lag_days),
non per "ultimi N giorni" come in insider_trading. Il calcolo dipende solo da
today+lag (non dal watermark): un run saltato non perde dati, il trimestre
viene ripreso nei run successivi finché processabile.

Avvertenze:
- ``value_usd`` è il valore GREZZO riportato dal filer (la regola SEC è in
  migliaia di dollari, ma alcuni filer depositano in dollari): unità
  filer-dipendente, nessuna conversione applicata.
- Le posizioni su OPZIONI (``putCall``) si scartano per default.
- Le righe con ``sshPrnamtType`` diverso da ``SH`` (principal amount:
  obbligazioni, fondi, trust) si scartano: non sono posizioni azionarie.
- I depositi il cui ``period_ending`` non combacia con il trimestre della
  finestra (amended 13F-HR/A tardivi, periodi anomali) si scartano: il
  trimestre è quello del FILING, non quello della finestra di deposito.
- Le righe con CUSIP non risolvibile NON si inseriscono: restano in
  ``cusip_lookup`` (resolved_at NULL) e si ritentano al run successivo.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from core.module_interface import ModuleInterface, ModuleResult, RunContext
from modules.institutional_holdings import cusip_map, edgar_13f

logger = logging.getLogger(__name__)

_MAX_LOOKBACK_QUARTERS = 400  # rete di sicurezza (100 anni) su lag assurdi


@dataclass(frozen=True)
class QuarterWindow:
    """Un trimestre 13F e la finestra di DEPOSITO dei suoi filing."""

    quarter: str          # '2026Q2'
    deposit_start: date   # q_end + 1
    deposit_end: date     # q_end + lag_days


def _quarter_end(year: int, quarter: int) -> date:
    if quarter == 4:
        return date(year, 12, 31)
    return date(year, 3 * quarter + 1, 1) - timedelta(days=1)


def target_quarters(today: date, lag_days: int, quarters_back: int) -> list[QuarterWindow]:
    """Trimestri chiusi da almeno ``lag_days``, più recente prima.

    Determinismo totale: dipende solo da today+lag, quindi un trimestre
    saltato (run mancanti) viene recuperato automaticamente dai run dopo.
    """
    windows: list[QuarterWindow] = []
    quarter = (today.month - 1) // 3 + 1
    year = today.year
    examined = 0
    while len(windows) < quarters_back and examined < _MAX_LOOKBACK_QUARTERS:
        examined += 1
        end = _quarter_end(year, quarter)
        if (today - end).days > lag_days:
            windows.append(
                QuarterWindow(
                    quarter=f"{year}Q{quarter}",
                    deposit_start=end + timedelta(days=1),
                    deposit_end=end + timedelta(days=lag_days),
                )
            )
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
    return windows


def period_matches_quarter(period_ending: str, quarter: str) -> bool:
    """Il ``period_ending`` EFTS combacia con il trimestre della finestra?

    Serve a scartare i depositi "fuori stagione" (amended 13F-HR/A depositati
    mesi dopo, o 13F con periodi anomali): senza questo controllo verrebbero
    etichettati con il trimestre della FINESTRA DI DEPOSITO, non con quello
    realmente coperto dal filing. Un ``period_ending`` vuoto non viene
    considerato un errore: si accetta (deve decidere chi chiama).
    """
    if not period_ending:
        return True
    try:
        reported = date.fromisoformat(period_ending[:10])
    except ValueError:
        logger.warning("period_ending non interpretabile: %r", period_ending)
        return True
    return f"{reported.year}Q{(reported.month - 1) // 3 + 1}" == quarter


def select_filings(filings: list[edgar_13f.Filing13F], max_filings: int,
                   whitelist: set[str]) -> tuple[list[edgar_13f.Filing13F], int]:
    """Applica il budget: prima i filer in whitelist, poi gli altri.

    L'ordine in ingresso è quello di EFTS (depositi più recenti): con la
    whitelist vuota si comporta quindi come "primi N più recenti".
    Ritorna (selezionati, quanti selezionati erano in whitelist).
    """
    if not whitelist:
        return list(filings[:max_filings]), 0

    def _in_wl(filing: edgar_13f.Filing13F) -> bool:
        return bool(filing.ciks) and filing.ciks[0] in whitelist

    ordered = sorted(filings, key=lambda f: 0 if _in_wl(f) else 1)  # sort stabile
    selected = ordered[:max_filings]
    return selected, sum(1 for f in selected if _in_wl(f))


class Module(ModuleInterface):
    key = "institutional_holdings"
    display_name = "Institutional holdings (SEC 13F-HR)"

    def run(self, ctx: RunContext) -> ModuleResult:
        user_agent = ctx.env("SEC_EDGAR_USER_AGENT") or edgar_13f.DEFAULT_USER_AGENT
        lag_days = int(ctx.get("lag_days", 45))
        quarters_back = int(ctx.get("quarters_back", 2))
        max_filings = int(ctx.get("max_filings", 120))
        max_filings_scan = int(ctx.get("max_filings_scan", 2000))
        skip_put_call = bool(ctx.get("skip_put_call", True))
        cusip_ttl_days = int(ctx.get("cusip_ttl_days", 60))
        core_matching = bool(ctx.get("core_name_matching", True))
        whitelist = {
            str(cik).strip().zfill(10)
            for cik in (ctx.get("filer_cik_filter") or [])
            if str(cik).strip()
        }

        today = date.today()
        windows = target_quarters(today, lag_days, quarters_back)
        if not windows:
            return ModuleResult(
                module_key=self.key,
                status="ok",
                rows_written=0,
                watermark=today.isoformat(),
                note=f"nessun trimestre processabile (lag_days={lag_days})",
            )

        errors: list[str] = []
        rows_written = 0
        found_total = processed = whitelist_used = 0
        skipped_put_call = skipped_no_shares = unresolved = 0
        skipped_not_shares = skipped_period = 0

        for window in windows:
            try:
                found = edgar_13f.search_13f(
                    user_agent,
                    window.deposit_start.isoformat(),
                    window.deposit_end.isoformat(),
                    max_filings_scan,
                )
            except edgar_13f.SecEdgarError as exc:
                return ModuleResult(
                    module_key=self.key, status="error",
                    errors=[f"13F {window.quarter}: ricerca EFTS fallita: {exc}"],
                )
            found_total += len(found)
            selected, in_wl = select_filings(found, max_filings, whitelist)
            whitelist_used += in_wl

            for filing in selected:
                processed += 1
                filer_cik = (filing.ciks[0] if filing.ciks else "") or None
                filer_name = (filing.display_names[0] if filing.display_names else "") or None

                if not period_matches_quarter(filing.period_ending, window.quarter):
                    skipped_period += 1
                    continue

                try:
                    info_url = edgar_13f.discover_information_table(
                        user_agent, filing.accession, filing.ciks
                    )
                    if info_url is None:
                        errors.append(f"13F {filing.accession}: information table non trovata")
                        continue
                    content = edgar_13f._get(info_url, user_agent).content
                    holdings = edgar_13f.parse_information_table(content)
                except edgar_13f.SecEdgarError as exc:
                    errors.append(f"13F {filing.accession}: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001 - un filing non deve mai
                    # far fallire l'intero run (XML/formato inattesi)
                    errors.append(f"13F {filing.accession}: errore inatteso: {exc!r}")
                    continue

                for row in holdings:
                    if not row.cusip:
                        continue
                    if skip_put_call and row.put_call:
                        skipped_put_call += 1
                        continue
                    if row.shares_type and row.shares_type != "SH":
                        # principal amount (obbligazioni, fondi, trust): non è una
                        # posizione azionaria, quindi niente score/delta azionario
                        skipped_not_shares += 1
                        continue
                    if not row.shares:
                        skipped_no_shares += 1
                        continue
                    try:
                        _ticker, company_id = cusip_map.resolve(
                            user_agent, ctx.conn, row.cusip, row.issuer_name,
                            ttl_days=cusip_ttl_days, core_matching=core_matching,
                        )
                    except Exception as exc:  # noqa: BLE001 - una riga non deve
                        # mai far fallire il filing (rete/DB/override manuale)
                        errors.append(f"13F {filing.accession} CUSIP {row.cusip}: {exc!r}")
                        continue
                    if company_id is None:
                        unresolved += 1
                        continue
                    prev = ctx.conn.execute(
                        """
                        SELECT shares FROM institutional_holdings
                        WHERE filer_cik IS ? AND cusip IS ? AND filing_quarter < ?
                        ORDER BY filing_quarter DESC LIMIT 1
                        """,
                        (filer_cik, row.cusip, window.quarter),
                    ).fetchone()
                    delta = (
                        row.shares - prev["shares"]
                        if prev is not None and prev["shares"] is not None
                        else None
                    )
                    cur = ctx.conn.execute(
                        """
                        INSERT OR IGNORE INTO institutional_holdings
                            (company_id, filing_quarter, filing_date, filer_name,
                             filer_cik, issuer_name, cusip, shares, value_usd,
                             shares_delta)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            company_id, window.quarter, filing.file_date or None,
                            filer_name, filer_cik, row.issuer_name or None,
                            row.cusip, row.shares, row.value, delta,
                        ),
                    )
                    rows_written += cur.rowcount

        note = (
            f"trimestri={','.join(w.quarter for w in windows)}; "
            f"depositi visti={found_total}, filing esaminati={processed} "
            f"(whitelist={whitelist_used}); opzioni scartate={skipped_put_call}, "
            f"non-SH scartate={skipped_not_shares}, senza quote={skipped_no_shares}, "
            f"periodo fuori finestra={skipped_period}, cusip non risolti={unresolved}; "
            f"value grezzo del filer (unità non normalizzata)"
        )
        if errors:
            note += f"; errori parziali={len(errors)}"
        return ModuleResult(
            module_key=self.key,
            status="ok",
            rows_written=rows_written,
            errors=errors[:20],
            watermark=today.isoformat(),
            note=note,
        )