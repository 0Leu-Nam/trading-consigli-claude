"""Extractor dei segnali grezzi per il modulo di scoring.

Ogni funzione ha la stessa firma e restituisce una lista di `Contribution`:
il contributo e' gia' pesato e descritto, ma non sa nulla del run, del DB o
della shortlist. Cosi' le regole restano testabili da sole e il modulo non deve
sapere nulla delle tabelle degli altri moduli.

Regola comune a tutti gli extractor: **restituiscono solo segnali esistenti**.
L'assenza di dati non e' un contributo zero, e' un elemento assente dalla
lista, e il modulo la distingue perché conta `module_key` distinti presenti.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.db import utcnow_iso


@dataclass(frozen=True)
class Contribution:
    """Un singolo segnale grezzo, gia' tradotto in contributo di punteggio.

    ``signal_type`` e' il nome della regola applicata (es. ``open_market_buy``)
    ed e' anche il valore scritto in ``signals.signal_type``.
    """

    module_key: str
    signal_type: str
    weight: float
    direction: int
    description: str


# Ogni extractor restituisce company_id -> [Contribution]. Il raggruppamento e'
# dentro l'extractor perche' e' li che conosce il join con la propria tabella.
Contributions = dict[int, list["Contribution"]]


def _fmt_usd(value: float | int | None) -> str:
    """Importo leggibile: 1_500_000_000 -> '1.5B'. Serve nelle description,
    che finiscono in tabella e poi nella dashboard."""
    if value is None:
        return "n/d"
    v = float(value)
    for suffix, scale in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(v) >= scale:
            return f"{v / scale:.1f}{suffix}"
    return f"{v:.0f}"


# ── insider_trading ────────────────────────────────────────────────────────────


def insider_signals(
    conn,
    *,
    days: int,
    min_value_usd: int,
    open_market_only: bool,
    weights: dict,
    threshold_types: dict,
) -> Contributions:
    """Acquisti e vendite open-market sopra soglia.

    Solo Form 4 realmente open-market: senza `is_open_market` il modulo mescola
    esercizi di opzioni,assegnazioni e conversioni, che non sono convinzione.
    Il segno segue il `transaction_type` (P acquisto, S vendita), mai la
    direzione del valore.
    """
    sql = """
        SELECT t.company_id,
               t.transaction_type,
               COUNT(*)          AS n_insider,
               SUM(t.value_usd)  AS value_usd
        FROM insider_transactions t
        WHERE t.filing_date >= date('now', ?)
          AND t.value_usd IS NOT NULL
          AND t.value_usd >= ?
          AND t.is_open_market = ?
        GROUP BY t.company_id, t.transaction_type
    """
    out: Contributions = {}
    for row in conn.execute(sql, (f"-{days} day", min_value_usd, 1 if open_market_only else 0)):
        kind = (row["transaction_type"] or "").upper()
        rule = threshold_types.get(kind)
        if rule is None or rule not in weights:
            # Tipo di transazione non mappato (M, F, A, ...): non e' un errore,
            # e' semplicemente una regola che questo modulo non applica.
            continue
        weight = float(weights[rule])
        side = "acquisto" if kind == "P" else "vendita"
        out.setdefault(row["company_id"], []).append(
            Contribution(
                module_key="insider_trading",
                signal_type=rule,
                weight=weight,
                direction=1 if weight >= 0 else -1,
                description=(
                    f"{side} open-market {_fmt_usd(row['value_usd'])} "
                    f"da {row['n_insider']} insider"
                ),
            )
        )
    return out


# ── price_screener ─────────────────────────────────────────────────────────────


def price_signals(conn, *, days: int, spike_mult: float, move_pct: float, weights: dict) -> Contributions:
    """Anomalie di prezzo/volume, con il segno del rendimento.

    ATTENZIONE: `abs_return_5d` contiene il RENDIMENTO con segno (min -0.85,
    max +1.74 su dati reali), non il suo valore assoluto. Filtrare con `ABS()`
    e poi premiare la soglia senza considerare il segno fa entrare metà
    dell'universo come anomalia: un ribasso del 10% e un rialzo del 10% hanno
    lo stesso valore assoluto e significati opposti. Qui la soglia viene
    confrontata con il valore con segno.

    Il join passa da `company_id` e non da `symbol`: `price_snapshots` ha
    `symbol` indicizzato ma i ticker fuori dall'universo del price_screener
    (es. societa' emerse solo dal Form 4) non hanno bar, e un join per symbol
    lascerebbe il contributo prezzo silenziosamente a None.
    """
    sql = """
        SELECT p.company_id,
               MAX(p.vol_vs_avg_20) AS vol_spike,
               MAX(p.abs_return_5d) AS move_5d
        FROM price_snapshots p
        WHERE p.date >= date('now', ?)
        GROUP BY p.company_id
    """
    out: Contributions = {}
    vol_rule, up_rule = "price_volume_spike", "price_move_up"
    for row in conn.execute(sql, (f"-{days} day",)):
        spike = row["vol_spike"]
        if spike is not None and spike >= spike_mult and vol_rule in weights:
            out.setdefault(row["company_id"], []).append(
                Contribution(
                    module_key="price_screener",
                    signal_type=vol_rule,
                    weight=float(weights[vol_rule]),
                    direction=1,
                    description=f"volume {spike:.1f}x la media 20gg (soglia {spike_mult}x)",
                )
            )
        move = row["move_5d"]
        # solo rialzo: un crollo e' un segnale, ma premiare il movimento
        # assoluto farebbe salire in shortlist anche i titoli che crollano
        if move is not None and move >= move_pct and up_rule in weights:
            out.setdefault(row["company_id"], []).append(
                Contribution(
                    module_key="price_screener",
                    signal_type=up_rule,
                    weight=float(weights[up_rule]),
                    direction=1,
                    description=f"rialzo 5gg {move * 100:+.1f}% (soglia {move_pct * 100:+.0f}%)",
                )
            )
    return out


# ── news_sentiment ─────────────────────────────────────────────────────────────


def news_signals(
    conn,
    *,
    days: int,
    positive_gte: float,
    negative_lte: float,
    min_articles: int,
    weights: dict,
) -> Contributions:
    """Sentiment medio per ticker, con un minimo di articoli.

    Il minimo conta perche' il sentiment e' rumore: su dati reali 711 articoli
    su 1082 sono `neutral`, e la media di un singolo titolo (+1.0) e' piu'
    estrema della media di dieci (+0.2). Senza soglia minima un titolo con una
    sola notizia batte un sentiment diffuso.
    """
    sql = """
        SELECT company_id,
               AVG(sentiment_score) AS avg_score,
               COUNT(*)             AS n_articles
        FROM news_events
        WHERE published_at >= datetime('now', ?)
          AND sentiment_score IS NOT NULL
        GROUP BY company_id
    """
    out: Contributions = {}
    pos_rule, neg_rule = "news_sentiment_positive", "news_sentiment_negative"
    for row in conn.execute(sql, (f"-{days} day",)):
        if row["n_articles"] < min_articles:
            continue
        avg = row["avg_score"]
        if avg >= positive_gte and pos_rule in weights:
            out.setdefault(row["company_id"], []).append(
                Contribution(
                    module_key="news_sentiment",
                    signal_type=pos_rule,
                    weight=float(weights[pos_rule]),
                    direction=1,
                    description=(
                        f"sentiment medio {avg:+.2f} su {row['n_articles']} articoli"
                    ),
                )
            )
        elif avg <= negative_lte and neg_rule in weights:
            out.setdefault(row["company_id"], []).append(
                Contribution(
                    module_key="news_sentiment",
                    signal_type=neg_rule,
                    weight=float(weights[neg_rule]),
                    direction=-1,
                    description=(
                        f"sentiment medio {avg:+.2f} su {row['n_articles']} articoli"
                    ),
                )
            )
    return out


# ── institutional_holdings ─────────────────────────────────────────────────────


def institutional_signals(
    conn,
    *,
    quarters_back: int,
    max_age_days: int,
    today: date,
    weights: dict,
) -> Contributions:
    """Nuove posizioni e aumenti dei gestori whitelistati, trimestre su trimestre.

    Perche' il calcolo e' qui e non letto da `institutional_holdings.shares_delta`:
    quella colonna e' NULL su tutto il DB. Il modulo 13F la calcola confrontando
    con il trimestre precedente *dentro lo stesso run* (module.py:278-290), ma
    con `quarters_back: 2` il primo trimestre processato non trova un
    precedente e resta NULL. Qui si confrontano i due trimestri gia' presenti
    con due query, quindi funziona dal primo run.

    `max_age_days` rende la freschezza esplicita: un 13F vecchio di 60 giorni e'
    il segnale normale (i 13F hanno 45-90 giorni di lag strutturale), non un
    errore, ma l'eta' finisce nella description cosi' in dashboard e' chiaro.
    """
    # I trimestri si prendono da quelli PRESENTI in tabella, non calcolati da
    # oggi: il modulo 13F ha un lag di 60gg e potrebbe non aver depositato
    # ancora l'ultimo trimestre chiuso. Usare il massimo effettivamente
    # salvato evita di confrontare due trimestri in cui uno non esiste.
    present = [
        r["filing_quarter"]
        for r in conn.execute(
            "SELECT DISTINCT filing_quarter FROM institutional_holdings"
            " WHERE filing_quarter IS NOT NULL ORDER BY filing_quarter DESC LIMIT ?",
            (quarters_back + 1,),
        )
    ]
    if len(present) < 2:
        # serve un trimestre precedente per parlare di "aumento"
        return {}

    latest, prev = present[0], present[1]
    age_days = _quarter_age_days(latest, today)
    if age_days > max_age_days:
        return {}

    age_note = f"13F {latest}, ~{age_days}gg fa"

    # posizioni NUOVE e AUMENTI: confronto trimestre corrente vs precedente
    # sullo stesso (filer_cik, cusip). Una posizione che sparisce non e' un
    # segnale per lo scoring: la sua assenza e' rumore, non una decisione.
    sql = """
        SELECT a.company_id, a.filer_cik, a.filer_name, a.cusip, a.shares,
               b.shares AS prev_shares
        FROM institutional_holdings a
        LEFT JOIN institutional_holdings b
               ON b.filer_cik = a.filer_cik
              AND b.cusip = a.cusip
              AND b.filing_quarter = ?
        WHERE a.filing_quarter = ?
    """
    new_rule, inc_rule, multi_rule = (
        "institutional_new_position",
        "institutional_increase",
        "institutional_multiple",
    )
    # (filer_cik, contributo): serve il CIK per contare i gestori DISTINTI,
    # altrimenti due posizioni dello stesso gestore sembrano due gestori
    out: Contributions = {}
    by_company: dict[int, list[tuple[str, Contribution]]] = {}
    for row in conn.execute(sql, (prev, latest)):
        old = row["prev_shares"]
        if old is None:
            rule, what = new_rule, "nuova posizione"
        elif row["shares"] is not None and old is not None and row["shares"] > old:
            rule, what = inc_rule, f"aumento {old:,}->{row['shares']:,} azioni"
        else:
            continue
        if rule not in weights:
            continue
        by_company.setdefault(row["company_id"], []).append(
            (
                row["filer_cik"] or "",
                Contribution(
                    module_key="institutional_holdings",
                    signal_type=rule,
                    weight=float(weights[rule]),
                    direction=1,
                    description=(
                        f"{what} per {(row['filer_name'] or row['filer_cik'] or '?')} ({age_note})"
                    ),
                ),
            )
        )

    # bonus di convergenza: piu' gestori whitelistati che aprono o aumentano la
    # stessa posizione e' un segnale piu' forte di un gestore solo
    if multi_rule in weights:
        for company_id, entries in by_company.items():
            distinct_filers = {cik for cik, _ in entries}
            if len(distinct_filers) > 1:
                entries.append(
                    (
                        "",
                        Contribution(
                            module_key="institutional_holdings",
                            signal_type=multi_rule,
                            weight=float(weights[multi_rule]),
                            direction=1,
                            description=(
                                f"{len(distinct_filers)} gestori whitelistati sulla posizione"
                            ),
                        ),
                    )
                )

    for company_id, entries in by_company.items():
        out.setdefault(company_id, []).extend(c for _, c in entries)
    return out


def _quarter_age_days(quarter: str, today: date) -> int:
    """Giorni dalla fine del trimestre indicato a oggi. E' l'eta' minima del
    dato: un 13F viene depositata fino a 45gg dopo la fine del trimestre, e il
    modulo la raccoglie con 60gg di lag."""
    year, q = (int(x) for x in quarter.replace("Q", " ").split())
    end_month = q * 3
    end = date(year, end_month, 31 if end_month in (3, 12) else 30)
    return (today - end).days


__all__ = [
    "Contribution",
    "Contributions",
    "insider_signals",
    "price_signals",
    "news_signals",
    "institutional_signals",
    "utcnow_iso",
]