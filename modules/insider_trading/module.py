"""Modulo insider_trading: scopre i Form 4 SEC e li salva in insider_transactions.

Solo raccolta/segnalazione, nessun ordine. Idempotente grazie a
UNIQUE(accession, row_no) + INSERT OR IGNORE: un secondo run non duplica.
"""

import logging
from datetime import date, timedelta

from core import db
from core.module_interface import ModuleInterface, ModuleResult, RunContext
from modules.insider_trading import sec_edgar

logger = logging.getLogger(__name__)

OPEN_MARKET_CODES = ("P", "S")


class Module(ModuleInterface):
    key = "insider_trading"
    display_name = "Insider trading (SEC Form 4)"

    def run(self, ctx: RunContext) -> ModuleResult:
        user_agent = ctx.env("SEC_EDGAR_USER_AGENT") or sec_edgar.DEFAULT_USER_AGENT
        lookback_days = int(ctx.get("lookback_days", 7))
        min_value_usd = int(ctx.get("min_value_usd", 0))
        open_market_only = bool(ctx.get("open_market_only", False))
        max_filings = int(ctx.get("max_filings", 250))

        end = date.today()
        start = end - timedelta(days=lookback_days)
        errors: list[str] = []

        try:
            filings = sec_edgar.search_form4(user_agent, start.isoformat(), end.isoformat(), max_filings)
        except sec_edgar.SecEdgarError as exc:
            return ModuleResult(module_key=self.key, status="error", errors=[str(exc)])

        transactions = 0
        skipped_no_ticker = 0
        processed = 0
        for filing in filings:
            if processed >= max_filings:
                break
            processed += 1
            try:
                xml_url = sec_edgar.fetch_ownership_xml_url(user_agent, filing)
                if xml_url is None:
                    errors.append(f"filing {filing.accession}: documento ownership non trovato")
                    continue
                xml_bytes = sec_edgar._get(xml_url, user_agent).content
                ticker, company_name, company_cik, rows = sec_edgar.parse_form4(
                    xml_bytes, xml_url, filing.accession
                )
            except sec_edgar.SecEdgarError as exc:
                errors.append(f"filing {filing.accession}: {exc}")
                continue

            if not ticker:
                skipped_no_ticker += 1
                continue

            company_id = db.upsert_company(
                ctx.conn, ticker, name=company_name or None, cik=company_cik or None
            )
            for tx in rows:
                if not tx.shares:
                    continue
                if open_market_only and tx.transaction_type not in OPEN_MARKET_CODES:
                    continue
                value = tx.value_usd or 0
                if value < min_value_usd:
                    continue
                row = (
                    company_id,
                    tx.accession,
                    tx.row_no,
                    tx.filing_date,
                    tx.transaction_date,
                    tx.insider_name,
                    tx.insider_title,
                    tx.transaction_type,
                    tx.shares,
                    tx.price_per_share,
                    value,
                    tx.holdings_after,
                    1 if tx.is_open_market else 0,
                    tx.url,
                )
                cur = ctx.conn.execute(
                    """
                    INSERT OR IGNORE INTO insider_transactions
                        (company_id, accession, row_no, filing_date, transaction_date,
                         insider_name, insider_title, transaction_type, shares,
                         price_per_share, value_usd, holdings_after, is_open_market, url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                transactions += cur.rowcount

        window = f"{start.isoformat()}..{end.isoformat()}"
        note = (
            f"filing esaminati={processed}, ignorati senza ticker={skipped_no_ticker}; "
            f"filtri: min_value_usd={min_value_usd}, open_market_only={open_market_only}"
        )
        if errors:
            note += f"; errori parziali={len(errors)}"
        return ModuleResult(
            module_key=self.key,
            status="ok",
            rows_written=transactions,
            errors=errors[:20],  # log compatti: non inondare run_log/console
            watermark=end.isoformat(),
            note=note,
        )