"""Extractor dei segnali grezzi per il modulo di scoring.

Ogni funzione ha la stessa firma e restituisce una lista di `Contribution`:
il contributo e' gia' pesato e descritto, ma non sa nulla del run, del DB o
della shortlist. Cosi' le regole restano testabili da sole e il modulo non deve
sapere nulla delle tabelle degli altri moduli.

Regola comune a tutti gli extractor: **restituiscono solo segnali esistenti**.
L'assenza di dati non e' un contributo zero, e' un elemento assente dalla
lista, e il modulo la distingue perché conta `module_key` distinti presenti.

Seconda regola, altrettanto importante: **ogni finestra e' ancorata alla data
massima della propria tabella sorgente, non a `date('now')`**. Con l'orologio di
sistema la stessa finestra dava risultati diversi a poche ore di distanza sullo
stesso identico dataset (misurato: la shortlist e' cambiata da sola tra due run
nella stessa giornata), e `INSERT OR IGNORE` conservava la riga vecchia: quello
che si leggeva non era quello che il codice avrebbe prodotto. Ancorando alla
tabella il risultato e' funzione del contenuto del DB, e una tabella che non
riceve piu' dati non sposta la finestra.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

Bands = tuple[tuple[float, float], ...]


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


@dataclass(frozen=True)
class InstitutionalResult:
    """I 13F hanno una particolarita' rispetto agli altri moduli: portano con
    se' l'eta' del deposito, e senza gate di freschezza (deciso: l'informazione
    resta visibile, non blocca) il modulo deve poter dire qual e' l'eta' massima
    usata nel run. Va nella `note`, non in `errors`: e' una cosa da sorvegliare,
    non un guasto.

    Il campo si chiama `max_deposit_age_days` e non `max_age_days` per non
    riportare in giro il nome del gate di freschezza che è stato tolto: è il
    massimo **osservato**, non una soglia.
    """

    contributions: dict[int, list[Contribution]]
    max_deposit_age_days: int | None = None
    quarters: tuple[str, ...] = ()


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


def _mult(value: float, bands: Bands) -> float:
    """Moltiplicatore dalla prima banda satisfied. L'ultima banda e' il piano:
    con `[{gte: .5, mult: 1.6}, ..., {gte: 0, mult: 0.7}]` il valore 0.03
    prende 0.7 e non resta senza moltiplicatore.
    """
    for threshold, mult in bands:
        if value >= threshold:
            return mult
    return bands[-1][1] if bands else 1.0


# ── insider_trading ────────────────────────────────────────────────────────────


def insider_signals(
    conn,
    *,
    days: int,
    min_value_usd: int,
    open_market_only: bool,
    weights: dict,
    threshold_types: dict,
    frac_buy: Bands,
    frac_sell: Bands,
    count_bands: Bands,
    usd_fallback_buy: Bands,
    usd_fallback_sell: Bands,
    max_abs: float,
) -> Contributions:
    """Acquisti e vendite open-market, pesati sulla dimensione reale.

    Solo Form 4 realmente open-market: senza `is_open_market` il modulo mescola
    esercizi di opzioni, assegnazioni e conversioni, che non sono convinzione.

    **Perche' un peso fisso non regge, misurato sulla settimana reale:**

        KOD   P  156.7M  1 insider   0.6% della posizione   -> era +30 (massimo)
        ADRX  P   33.3M  3 insider  60.7% della posizione   -> era +30 (uguale)
        MPWR  S   40.3M  1 insider   0.8% della posizione   -> era -20
        CX     S    8.9M  1 insider  61.3% della posizione   -> era -20

    Un taglio di 0.6% della propria posizione e una vendita del 61% di quella
    posizione sono due eventi opposti e avevano lo stesso peso. Il contributo
    e' quindi `peso_base x banda(frazione di posizione) x banda(n. insider)`,
    limitato a `max_abs`: la frazione e' l'unica misura confrontabile tra una
    microcap e una mega-cap, perche' il valore in dollari assoluto no.

    La frazione e' media PONDERATA PER VALORE fra le dichiarazioni con
    `holdings_after`: un direttore che vende tutta la sua posizione conta per
    quello che muove, non quanto sono numerose le sue micro-vendite. Le righe
    senza `holdings_after` (2.7% del valore nella finestra reale) non entrano
    nel denominatore e il peso cade sulla banda USD di fallback.

    `holdings_after = 0` NON e' dato mancante: e' l'uscita completa, cioe' una
    vendita del 100% della posizione, e il suo peso e' quello piu' alto della
    banda. Confonderlo con un NULL avrebbe fatto cadere le uscite complete sul
    fallback per importo, cioe' il caso piu' forte pesato come il piu' debole.

    `n_insider` conta persone DISTINTE (`COUNT(DISTINCT insider_name)`), non
    righe di Form 4: CBRS ha 36 dichiarazioni da 3 persone, e la description
    riporta entrambi i numeri per non confonderli.
    """
    anchor = conn.execute("SELECT MAX(filing_date) AS d FROM insider_transactions").fetchone()["d"]
    if not anchor:
        return {}

    sql = """
        SELECT company_id,
               transaction_type,
               COUNT(DISTINCT insider_name) AS n_insider,
               COUNT(*)                      AS n_rows,
               SUM(value_usd)                AS value_usd,
               SUM(CASE WHEN frac_ok = 1 THEN value_usd * frac END) AS frac_num,
               SUM(CASE WHEN frac_ok = 1 THEN value_usd ELSE 0 END) AS frac_den
        FROM (
            SELECT t.company_id, t.transaction_type, t.insider_name,
                   t.value_usd,
                   CASE WHEN t.transaction_type = 'S'
                        THEN t.shares * 1.0 / (t.shares + t.holdings_after)
                        ELSE t.shares * 1.0 / t.holdings_after END      AS frac,
                   CASE WHEN t.shares IS NOT NULL
                         AND t.holdings_after IS NOT NULL
                         AND CASE WHEN t.transaction_type = 'S'
                                  THEN t.shares + t.holdings_after
                                  ELSE t.holdings_after END > 0
                        THEN 1 ELSE 0 END                              AS frac_ok
            FROM insider_transactions t
            WHERE t.filing_date >= date(?, ?)
              AND t.value_usd IS NOT NULL
              AND t.value_usd >= ?
              AND t.is_open_market = ?
        )
        GROUP BY company_id, transaction_type
    """
    out: Contributions = {}
    for row in conn.execute(sql, (anchor, f"-{days} day", min_value_usd, 1 if open_market_only else 0)):
        kind = (row["transaction_type"] or "").upper()
        rule = threshold_types.get(kind)
        if rule is None or rule not in weights:
            # Tipo di transazione non mappato (M, F, A, ...): non e' un errore,
            # e' semplicemente una regola che questo modulo non applica.
            continue
        base = float(weights[rule])
        value = float(row["value_usd"] or 0.0)
        frac = (row["frac_num"] / row["frac_den"]) if row["frac_den"] else None

        if frac is not None:
            frac_bands = frac_buy if kind == "P" else frac_sell
            size_mult = _mult(frac, frac_bands)
            frac_note = f", {frac * 100:.1f}% delle posizioni"
            if row["frac_den"] < value:
                frac_note += f" (su {_pct(row['frac_den'], value)} del valore)"
        else:
            fallback = usd_fallback_buy if kind == "P" else usd_fallback_sell
            size_mult = _mult(value, fallback)
            frac_note = ", frazione n/d (peso per valore)"

        n_insider = row["n_insider"] or 0
        weight = max(-max_abs, min(max_abs, base * size_mult * _mult(float(n_insider), count_bands)))
        side = "acquisto" if kind == "P" else "vendita"
        people = "1 insider distinto" if n_insider == 1 else f"{n_insider} insider distinti"
        if row["n_rows"] and row["n_rows"] > n_insider:
            people += f" su {row['n_rows']} dichiarazioni"
        out.setdefault(row["company_id"], []).append(
            Contribution(
                module_key="insider_trading",
                signal_type=rule,
                weight=weight,
                direction=1 if weight >= 0 else -1,
                description=f"{side} open-market {_fmt_usd(value)} da {people}{frac_note}",
            )
        )
    return out


def _pct(part: float, whole: float) -> str:
    """Quota percentuale leggibile, con una cifra: 0.973 -> '97%'."""
    if not whole:
        return "0%"
    return f"{part / whole * 100:.0f}%"


# ── price_screener ─────────────────────────────────────────────────────────────


def price_signals(conn, *, days: int, spike_mult: float, move_pct: float, weights: dict) -> Contributions:
    """Anomalie di prezzo/volume, con il segno del rendimento.

    ATTENZIONE: `abs_return_5d` contiene il RENDIMENTO con segno (min -0.85,
    max +1.74 su dati reali), non il suo valore assoluto. Filtrare con `ABS()`
    e poi premiare la soglia senza considerare il segno fa entrare metà
    dell'universo come anomalia: un ribasso del 10% e un rialzo del 10% hanno
    lo stesso valore assoluto e significati opposti. Qui la soglia viene
    confrontata con il valore con segno.

    La finestra e' ancorata all'ultima barra presente in tabella: il prezzo non
    viene piu' da un clock, ma dal dato.

    Il join passa da `company_id` e non da `symbol`: `price_snapshots` ha
    `symbol` indicizzato ma i ticker fuori dall'universo del price_screener
    (es. societa' emerse solo dal Form 4) non hanno bar, e un join per symbol
    lascerebbe il contributo prezzo silenziosamente a None.
    """
    anchor = conn.execute("SELECT MAX(date) AS d FROM price_snapshots").fetchone()["d"]
    if not anchor:
        return {}

    sql = """
        SELECT p.company_id,
               MAX(p.vol_vs_avg_20) AS vol_spike,
               MAX(p.abs_return_5d) AS move_5d
        FROM price_snapshots p
        WHERE p.date >= date(?, ?)
        GROUP BY p.company_id
    """
    out: Contributions = {}
    vol_rule, up_rule = "price_volume_spike", "price_move_up"
    for row in conn.execute(sql, (anchor, f"-{days} day")):
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

    Il confronto passa da `datetime(published_at)`, non dal confronto di
    stringhe: `published_at` e' ISO-8601 con 'T' e offset (`...T09:00:00+00:00`)
    mentre `datetime()` produce 'YYYY-MM-DD HH:MM:SS', e confrontarle a parole
    ('T' > ' ') includeva articoli piu' vecchi della soglia di qualche ora, con
    effetti al bordo della finestra.
    """
    anchor = conn.execute("SELECT MAX(published_at) AS d FROM news_events").fetchone()["d"]
    if not anchor:
        return {}

    sql = """
        SELECT company_id,
               AVG(sentiment_score) AS avg_score,
               COUNT(*)             AS n_articles
        FROM news_events
        WHERE datetime(published_at) >= datetime(?, ?)
          AND sentiment_score IS NOT NULL
        GROUP BY company_id
    """
    out: Contributions = {}
    pos_rule, neg_rule = "news_sentiment_positive", "news_sentiment_negative"
    for row in conn.execute(sql, (anchor, f"-{days} day")):
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
    today: date,
    weights: dict,
) -> InstitutionalResult:
    """Nuove posizioni e aumenti dei gestori whitelistati, trimestre su trimestre.

    Perche' il calcolo e' qui e non letto da `institutional_holdings.shares_delta`:
    quella colonna e' NULL su tutto il DB. Il modulo 13F la calcola confrontando
    con il trimestre precedente *dentro lo stesso run* (module.py:278-290), ma
    con `quarters_back: 2` il primo trimestre processato non trova un
    precedente e resta NULL. Qui si confrontano i due trimestri gia' presenti
    con due query, quindi funziona dal primo run.

    **L'eta' e' quella del DEPOSITO (`filing_date`), non quella della chiusura
    del trimestre.** La versione precedente contava i giorni dalla fine del
    trimestre e su dati reali dichiarava `~96gg fa` per una 13F del 2026Q2
    depositata il 2026-08-14: 51 giorni. Lo scopo dell'informazione e' sapere
    quanto e' fresco il *fatto* che si sta usando, e la data in cui il
    documento e' comparso e' l'unica che lo dice. Su un DB reale lo stesso
    trimestre si presenta con due date di deposito diverse (2026-05-15 e
    2026-05-18, emendamenti arrivati in momenti diversi): per questo la data e'
    letta riga per riga e non calcolata una volta sola per il trimestre.

    **Non c'e' un gate di freschezza.** Un 13F vecchio resta un'informazione
    vera sul gestore, e' solo vecchia: scartarla nasconderebbe il fatto che il
    modulo non sta depositando, che e' esattamente cio' che serve vedere. Il
    costo e' accettato e reso visibile: la soglia di AVVERTIMENTO sta nel modulo
    (`warn_institutional_age_days`) e finisce in `note`, non come condizione che
    spegne i segnali.
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
        return InstitutionalResult({}, None, ())

    latest, prev = present[0], present[1]

    # posizioni NUOVE e AUMENTI: confronto trimestre corrente vs precedente
    # sullo stesso (filer_cik, cusip). Una posizione che sparisce non e' un
    # segnale per lo scoring: la sua assenza e' rumore, non una decisione.
    sql = """
        SELECT a.company_id, a.filer_cik, a.filer_name, a.cusip, a.shares,
               b.shares AS prev_shares, a.filing_date
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
    ages: list[int] = []
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
        age_note, age_days = _deposit_age(row["filing_date"], latest, today)
        if age_days >= 0:
            ages.append(age_days)
        by_company.setdefault(row["company_id"], []).append(
            (
                row["filer_cik"] or "",
                Contribution(
                    module_key="institutional_holdings",
                    signal_type=rule,
                    weight=float(weights[rule]),
                    direction=1,
                    description=f"{what} per {(row['filer_name'] or row['filer_cik'] or '?')} ({age_note})",
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
    return InstitutionalResult(out, max(ages) if ages else None, (latest, prev))


def _deposit_age(filing_date: str | None, quarter: str, today: date) -> tuple[str, int]:
    """Description dell'eta' del deposito e i giorni, per riga.

    Se `filing_date` manca non si inventa un'eta': si dichiara il trimestre e
    si dice che la data non c'e'. Restituisce anche i giorni cosi' il modulo
    puo' tenere il massimo e avvisare in `note` senza rileggere le righe.
    """
    if not filing_date:
        return f"13F {quarter}, data deposito n/d", -1
    filed = date.fromisoformat(filing_date[:10])
    days = max((today - filed).days, 0)
    return f"13F {quarter} depositato {filed.isoformat()}, ~{days}gg fa", days


__all__ = [
    "Contribution",
    "Contributions",
    "InstitutionalResult",
    "Bands",
    "insider_signals",
    "price_signals",
    "news_signals",
    "institutional_signals",
]