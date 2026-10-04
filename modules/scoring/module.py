"""Fase 6: scoring e aggregazione dei segnali.

Il modulo NON raccoglie dati: legge quello che gli altri 4 moduli hanno gia'
scritto in tabella e lo combina in un punteggio per ticker.

Tre scelte strutturali, perche' ognuna evita una classe di errore silenzioso:

1. **Un modulo senza dati non contribuisce e non penalizza.** Ogni riga in
   `signals` registra `copertura` (quanti moduli hanno parlato su quanti
   possibili) e `contributi` (quali). Misurato sul DB reale: nessuna company ha
   tutti e 4 i segnali, 122 ne hanno 2, 15 ne hanno 3. Un modulo assente e'
   quindi il caso normale, non l'eccezione, e trattarlo come segnale negativo
   (o normalizzare per moduli disponibili) falserebbe il confronto fra ticker.

2. **Ogni modulo ha la sua finestra.** L'istituzionale e' trimestrale con 45-90
   giorni di lag strutturale: paragonarlo a una finestra settimanale premieria
   posizioni vecchie come se fossero notizie. L'eta' del dato (quella del
   deposito) finisce nella description, e **non c'e' un gate di freschezza**:
   un 13F vecchio e' un'informazione vera, e scartarla nasconderebbe il fatto
   che il modulo 13F ha smesso di depositare.

3. **Ogni finestra e' ancorata alla tabella, non all'orologio.** Ogni finestra
   parte dal massimo della propria tabella sorgente (`MAX(filing_date)`,
   `MAX(date)`, `MAX(published_at)`), cosi' la stessa copia del DB produce la
   stessa shortlist in qualsiasi momento. Con `date('now')` la finestra si
   spostava da sola durante il giorno e la riga letta in tabella poteva non
   essere quella che il codice aveva prodotto.

4. **Il punteggio e' tracciabile riga per riga.** Ogni contributo genera una
   riga in `signals` con il suo peso e il perche', piu' una riga di sintesi
   con il totale. La Fase 7 puo' mostrare i dettagli con due query, senza
   parsing di JSON e senza dover ricalcolare nulla.

5. **Il peso misura la dimensione dell'evento.** I pesi insider sono basi, non
   valori finali: il contributo e' `peso_base x banda(frazione di posizione) x
   banda(numero di persone distinte)`. Con un peso fisso, un taglio dello 0.6%
   della propria posizione e una vendita del 61% di quella posizione avevano
   lo stesso peso (misurato su KOD e CX).
"""
from __future__ import annotations

import logging
from datetime import date

from core.db import utcnow_iso
from core.module_interface import ModuleInterface, ModuleResult, RunContext
from modules.scoring import sources

log = logging.getLogger(__name__)

# I moduli che possono contribuire, nell'ordine in cui finiscono nella
# description: l'ordine e' fissato qui e non letto dal DB, altrimenti lo stesso
# input produrrebbe due righe diverse a seconda dell'ordine delle query.
SOURCE_MODULES = (
    "insider_trading",
    "price_screener",
    "news_sentiment",
    "institutional_holdings",
)

# Regola applicata a ciascun tipo di transazione Form 4. La mappa e' esplicita
# invece di dedotta dal segno: "P" e' acquisto, "S" vendita, e gli altri tipi
# (M, F, A, C, D, G) sono esercizi, assegnazioni o donazioni, non convinzione.
TRANSACTION_TYPES = {"P": "insider_open_market_buy", "S": "insider_open_market_sell"}

# Etichetta dei ticker a fonte singola. Va nella **description della riga di
# sintesi**, non solo nella `note`: la Fase 7 legge la tabella e deve poter
# separare le due sezioni con una query, senza ricalcolare coperture ne' voti.
# "Da una sola fonte" e' un fatto, non un piazzamento in classifica: un ticker
# puo' portare l'etichetta anche se il tetto di `single_source_limit` lo tiene
# fuori dalla lista mostrata.
SINGLE_SOURCE_LABEL = "segnale da una sola fonte, nessuna conferma incrociata"


class Module(ModuleInterface):
    key = "scoring"
    display_name = "Scoring e shortlist candidati"

    def run(self, ctx: RunContext) -> ModuleResult:
        conf = ctx.module_config or {}
        weights = dict(conf.get("weights") or {})
        windows = dict(conf.get("windows") or {})
        min_signals = int(conf.get("min_signals", 2))
        shortlist_size = int(conf.get("shortlist_size", 25))
        recalc = bool(conf.get("recalc", False))
        conflict_min_weight = int(conf.get("conflict_min_weight", 10))
        scale = dict(conf.get("insider_scale") or {})
        warn_inst_age = int(conf.get("warn_institutional_age_days", 150))
        single_min = conf.get("single_source_min")
        single_min = None if single_min is None else float(single_min)
        single_limit = int(conf.get("single_source_limit", 10))

        if not weights:
            return ModuleResult(
                module_key=self.key,
                status="error",
                errors=["nessun peso configurato in modules.scoring.weights: niente da valutare"],
            )

        # La data del segnale e' l'ultima barra di prezzo presente, non
        # date.today(): cosi' un ricalcolo il giorno dopo sullo stesso dataset
        # produce la stessa chiave e INSERT OR IGNORE non scrive nulla, mentre
        # nuovi dati di prezzo spostano la chiave come negli altri moduli.
        row = ctx.conn.execute("SELECT MAX(date) AS d FROM price_snapshots").fetchone()
        signal_date = row["d"] if row else None
        if not signal_date:
            return ModuleResult(
                module_key=self.key,
                status="error",
                errors=["nessuna barra in price_snapshots: il pricing non ha girato"],
            )

        errors: list[str] = []
        ignored = self._ignored_tickers(ctx.conn)
        contributions, institutional = self._collect(
            ctx, weights, windows, scale, date.fromisoformat(signal_date), errors
        )

        by_company: dict[int, list[sources.Contribution]] = {}
        for company_id, items in contributions.items():
            if company_id in ignored:
                continue
            by_company.setdefault(company_id, []).extend(items)

        rows_written = 0
        scored: list[tuple[float, int, str, int]] = []
        now = utcnow_iso()
        for company_id, items in by_company.items():
            try:
                total = sum(c.weight for c in items)
                modules_seen = len({c.module_key for c in items})
                # il ticker della riga serve alla shortlist e al log
                ticker = ctx.conn.execute(
                    "SELECT ticker FROM companies WHERE id = ?", (company_id,)
                ).fetchone()["ticker"]
                covered = ",".join(sorted({c.module_key for c in items}))
                written = 0

                # Gli contributi si AGGREGANO per (module_key, signal_type)
                # prima di essere scritti. Il vincolo UNIQUE e'
                # (company_id, module_key, signal_type, signal_date): senza
                # aggregare, due aumenti dello stesso tipo (es. due gestori
                # whitelistati che aprono la stessa posizione) avrebbero la
                # stessa chiave e il secondo verrebbe scartato in silenzio da
                # INSERT OR IGNORE. Il peso si somma e le descrizioni si
                # concatenano, cosi' la riga resta completa.
                merged = _merge_contributions(items)

                # Contrasto: pesi positivi e negativi entrambi sopra soglia.
                # NON modifica il punteggio, lo rende leggibile: un +35 che
                # nasconde una vendita open-market da insider non e' la stessa
                # cosa di un +35 con due fonti d'accordo. Il disaccordo e' il
                # dato, e va detto anche quando la somma e' positiva.
                conflict = _conflict(merged, conflict_min_weight)

                for c in merged:
                    written += self._insert(
                        ctx.conn,
                        company_id=company_id,
                        signal_date=signal_date,
                        generated_at=now,
                        module_key=c.module_key,
                        signal_type=c.signal_type,
                        magnitude=c.weight,
                        direction=c.direction,
                        description=c.description,
                        recalc=recalc,
                    )

                # riga di sintesi: il totale e' la somma dei pesi effettivi e
                # la description dichiara la copertura, cosi' uno score 60 su 2
                # moduli non e' confrontabile con uno 60 su 4 senza leggerlo.
                # L'appartenenza a una sezione e' decisa qui, per ticker, senza
                # aspettare la classifica: dipende solo dalla copertura e dal
                # totale, entrambi noti. Il tetto di lista (`single_source_limit`)
                # e' un'altra cosa, e viene dopo.
                single_source = (
                    single_min is not None
                    and modules_seen < min_signals
                    and total >= single_min
                )
                written += self._insert(
                    ctx.conn,
                    company_id=company_id,
                    signal_date=signal_date,
                    generated_at=now,
                    module_key=self.key,
                    signal_type="composite",
                    magnitude=total,
                    direction=1 if total >= 0 else -1,
                    description=(
                        f"score {total:+.1f} da {len(merged)} contributi, "
                        f"copertura {modules_seen}/{len(SOURCE_MODULES)} ({covered})"
                        + (f"; {conflict}" if conflict else "")
                        + (f"; {SINGLE_SOURCE_LABEL}" if single_source else "")
                    ),
                    recalc=recalc,
                )
                rows_written += written
                scored.append((total, modules_seen, ticker, company_id, conflict))
            except Exception as exc:                       # noqa: BLE001
                # Fallimento isolato: un ticker che non si lascia valutare non
                # ferma gli altri e finisce in errors[], che con
                # partial_error_threshold=0 fa classificare il run 'warning'.
                # Un'assenza di dati non arriva qui: e' semplicemente un
                # contributo in meno.
                log.warning("scoring: ticker non valutato (company_id=%s): %s", company_id, exc)
                errors.append(f"scoring: company_id={company_id} non valutata: {exc}")

        multi, single = _shortlist_sections(
            scored, min_signals, shortlist_size, single_min, single_limit
        )

        if not scored:
            errors.append(
                "nessun ticker valutabile: i 4 moduli non hanno dati sovrapposti "
                "(o i ticker sono tutti in watchlist con status 'ignore')"
            )

        return ModuleResult(
            module_key=self.key,
            status="ok",
            rows_written=rows_written,
            errors=errors,
            watermark=signal_date,
            note=self._note(
                scored, multi, single, errors, ignored, institutional,
                warn_inst_age, single_min,
            ),
        )

    # ── raccolta ───────────────────────────────────────────────────────────────

    def _collect(
        self, ctx: RunContext, weights: dict, windows: dict, scale: dict,
        today: date, errors: list[str],
    ) -> tuple[dict[int, list[sources.Contribution]], sources.InstitutionalResult]:
        """Chiama i 4 extractor e raggruppa per company_id.

        Ogni extractor e' isolato: se il modulo insider solleva un errore, gli
        altri tre contribuiscono comunque e il run si classifica 'warning'
        invece di restare muto. Il contributo del modulo fallito manca, quindi
        la copertura dichiarata nella riga di sintesi non e' confrontabile con
        quella di un run senza errori: e' per questo che l'errore resta in
        errors[] e non viene solo loggato.

        `today` e' la data di segnale, non `date.today()`: e' il riferimento per
        l'eta' dei 13F, cosi' la description resta riproducibile sullo stesso
        dataset. Le finestre, invece, ognuna la prende dal massimo della propria
        tabella sorgente (vedi `sources`): una finestra ancorata all'orologio
        cambiava la shortlist da sola a poche ore di distanza.
        """
        out: dict[int, list[sources.Contribution]] = {}
        institutional = sources.InstitutionalResult({})
        win_ins = windows.get("insider", {})
        spec = (
            (
                "insider_trading",
                lambda: sources.insider_signals(
                    ctx.conn,
                    days=int(win_ins.get("days", 7)),
                    min_value_usd=int(win_ins.get("min_value_usd", 50_000)),
                    open_market_only=bool(win_ins.get("open_market_only", True)),
                    weights=weights,
                    threshold_types=TRANSACTION_TYPES,
                    frac_buy=_bands(scale.get("frac_buy")),
                    frac_sell=_bands(scale.get("frac_sell")),
                    count_bands=_bands(scale.get("by_insider_count")),
                    usd_fallback_buy=_bands(scale.get("usd_fallback", {}).get("buy")),
                    usd_fallback_sell=_bands(scale.get("usd_fallback", {}).get("sell")),
                    max_abs=float(scale.get("max_abs", 45)),
                ),
            ),
            (
                "price_screener",
                lambda: sources.price_signals(
                    ctx.conn,
                    days=int(windows.get("price", {}).get("days", 5)),
                    spike_mult=float(windows.get("price", {}).get("vol_spike_mult", 3.0)),
                    move_pct=float(windows.get("price", {}).get("move_5d_pct", 0.10)),
                    weights=weights,
                ),
            ),
            (
                "news_sentiment",
                lambda: sources.news_signals(
                    ctx.conn,
                    days=int(windows.get("news", {}).get("days", 14)),
                    positive_gte=float(windows.get("news", {}).get("positive_gte", 0.35)),
                    negative_lte=float(windows.get("news", {}).get("negative_lte", -0.35)),
                    min_articles=int(windows.get("news", {}).get("min_articles", 3)),
                    weights=weights,
                ),
            ),
            (
                "institutional_holdings",
                lambda: sources.institutional_signals(
                    ctx.conn,
                    quarters_back=int(windows.get("institutional", {}).get("quarters", 1)),
                    today=today,
                    weights=weights,
                ),
            ),
        )
        for module_key, run_extractor in spec:
            try:
                found = run_extractor()
            except Exception as exc:                       # noqa: BLE001
                errors.append(f"scoring: sorgente {module_key} non valutabile: {exc}")
                log.warning("scoring: sorgente %s non valutabile: %s", module_key, exc)
                continue
            if module_key == "institutional_holdings":
                institutional = found
                found = institutional.contributions
            for company_id, contributions in found.items():
                out.setdefault(company_id, []).extend(contributions)
        return out, institutional

    def _ignored_tickers(self, conn) -> set[int]:
        """company_id con status 'ignore' in watchlist. Un ticker ignorato non
        riceve punteggio e non compare in shortlist, anche con score alto."""
        rows = conn.execute(
            "SELECT company_id FROM watchlist WHERE status = 'ignore'"
        ).fetchall()
        return {r["company_id"] for r in rows}

    @staticmethod
    def _insert(
        conn, *, company_id, signal_date, generated_at, module_key, signal_type,
        magnitude, direction, description, recalc,
    ) -> int:
        """Scrive una riga in `signals`.

        `INSERT OR IGNORE` sul UNIQUE (company_id, module_key, signal_type,
        signal_date): coerente con gli altri moduli. Un run ripetuto sullo
        stesso dataset non duplica e non altera nulla; con `recalc: true` la
        sovrascrittura diventa esplicita. Il watermark storico non viene perso
        per errore, perche' ogni giorno ha la sua chiave.
        """
        verb = "INSERT OR REPLACE" if recalc else "INSERT OR IGNORE"
        cur = conn.execute(
            f"""
            {verb} INTO signals
                (company_id, generated_at, signal_date, module_key, signal_type,
                 magnitude, direction, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (company_id, generated_at, signal_date, module_key, signal_type,
             float(magnitude), int(direction), description),
        )
        return cur.rowcount

    @staticmethod
    def _note(scored, multi, single, errors, ignored, institutional, warn_age,
              single_min) -> str:
        # Ogni sezione ha la propria riga di top: sono due elenchi che non sono
        # confrontabili, e accostarli in una riga sola ("top: ...") farebbe
        # sembrare il +55 a fonte singola piu' convincente del +35 a due moduli.
        def _top(sezione):
            return ", ".join(f"{t}={s:+.0f}" for s, _n, t, _c, *_ in sezione[:5])

        base = (
            f"ticker valutati={len(scored)}, in shortlist multipla={len(multi)} "
            f"(min_signals), in shortlist fonte singola={len(single)} "
            + ("disabilitata" if single_min is None else f"(>={single_min:+.0f})")
            + f", ignorati da watchlist={len(ignored)}"
        )
        # Il conteggio dei contrasti va in note perche' la shortlist e' la lista
        # che l'utente legge per prima: se 4 dei primi 25 hanno segnali che si
        # contraddicono, e' un'informazione sul metodo, non sul singolo ticker.
        conflicts = [t for _s, _n, t, _c, cf in scored if cf]
        if conflicts:
            sample = ",".join(sorted(conflicts)[:5])
            base += (
                f"; segnali contrastanti={len(conflicts)} ({sample}"
                + ("..." if len(conflicts) > 5 else "")
                + ")"
            )
        base += _institutional_age_note(institutional, warn_age)
        if multi:
            base += f"; top multipla: {_top(multi)}"
        if single:
            base += f"; top fonte singola: {_top(single)}"
        if errors:
            base += f"; {len(errors)} problemi (vedi errors)"
        return base


def _institutional_age_note(institutional, warn_age: int) -> str:
    """Eta' del 13F piu' vecchio usato nel run: informazione, non blocco.

    Il gate di freschezza e' stato tolto di proposito (vedi
    `institutional_signals`), quindi il rischio residuo e' uno solo: che il
    modulo 13F smetta di depositare e che l'ultimo trimestre salvato continui a
    valere per sempre senza che nessuno se ne accorga. Qui si dice esplicitamente
    quando l'eta' supera la soglia, cosi' la fermata si vede nel log del run.

    Va in `note` e NON in `errors` per scelta: un dato vecchio non e' un
    fallimento, e un run che segnala errori perche' una fonte e' in ritardo
    finisce con `partial_error_threshold` e sembra rotto quando funziona. La
    soglia e' 150gg e non 180 perche' i 13F si depositano entro 45gg dalla fine
    del trimestre: 150 giorni sul deposito significa che sono mancati due
    trimestri di raccolta, non che il dato e' un po' in ritardo.
    """
    if institutional.max_deposit_age_days is None:
        return ""
    oldest = institutional.max_deposit_age_days
    quarters = "/".join(institutional.quarters)
    note = f"; 13F usati {quarters}, deposito piu' vecchio {oldest}gg"
    if oldest > warn_age:
        note += f" (> {warn_age}gg: il modulo 13F potrebbe non aver depositato)"
    return note


def _bands(raw) -> sources.Bands:
    """Da YAML a tuple (soglia, moltiplicatore, e_un_tetto), nell'ordine in cui
    sono scritte.

    Le soglie si scendono: la prima che il valore soddisfa vince, e l'ultima
    voce e' il piano (il valore piu' piccolo possibile ha comunque un peso).
    `gte_usd` e `gte` sono la stessa chiave con due unita' diverse (dollari o
    frazione di posizione), perche' nella config gli importi e le frazioni non
    possono finire nella stessa lista.

    Una banda puo' anche essere un **tetto** (`lt` invece di `gte`): serve a
    svalutare quello che sta sotto una soglia minima, perche' una quota di
    posizione vicina allo zero non e' un segnale debole, e' assenza di segnale.
    I tetti si valutano prima di tutte le altre bande, quindi una banda `lt` e'
    obbligata a essere la **prima** voce: metterla piu' in la' la renderebbe
    irraggiungibile (sotto la prima soglia si prende gia' il suo moltiplicatore),
    e una config che sembra funzionare ma non fa niente e' peggio di un errore.
    """
    if not raw:
        return ()
    bands: sources.Bands = ()
    for position, item in enumerate(raw):
        has_gte = "gte" in item or "gte_usd" in item
        has_lt = "lt" in item
        if has_gte and has_lt:
            raise ValueError(
                f"banda non valida {item!r}: 'gte' e 'lt' sulla stessa banda, "
                "una banda e' un tetto oppure un floor, non entrambi"
            )
        if not has_gte and not has_lt:
            raise ValueError(
                f"banda non valida {item!r}: servono 'gte' (o 'gte_usd') o 'lt', e 'mult'"
            )
        if "mult" not in item:
            raise ValueError(f"banda non valida {item!r}: manca 'mult'")
        if has_lt and position != 0:
            raise ValueError(
                f"banda non valida {item!r}: una banda 'lt' deve essere la prima, "
                f"e' la posizione {position}: sotto la soglia di un tetto "
                "precedente la banda successiva non viene mai valutata"
            )
        threshold = float(item["lt"] if has_lt else item.get("gte", item.get("gte_usd")))
        bands += ((threshold, float(item["mult"]), has_lt),)
    return bands


def _merge_contributions(items):
    """Unisce i contributi con lo stesso (module_key, signal_type) sommando i
    pesi e concatenando le descrizioni.

    Necessario perche' `signals` ha UNIQUE (company_id, module_key,
    signal_type, signal_date): senza aggregare, due contributi dello stesso
    tipo per lo stesso ticker condividerebbero la chiave e il secondo
    verrebbe scartato in silenzio da INSERT OR IGNORE, con il peso perso.

    L'ordine di uscita segue il primo avvenimento, cosi' lo stesso input
    produce sempre lo stesso elenco di righe.
    """
    order: list[tuple[str, str]] = []
    acc: dict[tuple[str, str], list] = {}
    for c in items:
        key = (c.module_key, c.signal_type)
        if key not in acc:
            acc[key] = [0.0, []]
            order.append(key)
        acc[key][0] += c.weight
        acc[key][1].append(c.description)
    return [
        sources.Contribution(
            module_key=module_key,
            signal_type=signal_type,
            weight=weight,
            direction=1 if weight >= 0 else -1,
            description="; ".join(descriptions),
        )
        for (module_key, signal_type), (weight, descriptions) in ((k, acc[k]) for k in order)
    ]


def _conflict(merged, min_weight: int) -> str:
    """Etichetta i contributi che vanno in direzioni opposte, senza toccare il
    punteggio.

    La soglia serve a non segnalare come conflitto il rumore: un
    `institutional_multiple` da +5 che si oppone a una vendita da -20 e' un
    segnale minore, non un disaccordo fra due fonti. Solo quando ENTRAMBI i
    lati superano `min_weight` il contrasto e' dichiarato.

    Caso reale (CBRS, 2026-10-02): `insider_open_market_sell` -20 (36 insider,
    87.7M) e `institutional_new_position` +50 (Altimeter) + `institutional_multiple`
    +5 finivano in un +35 apparentemente pieno, senza che nulla dicesse che
    metà dei contributi tirava dalla parte opposta.

    Restituisce "" se non c'e' contrasto, cosi' la description resta pulita nei
    casi normali.
    """
    positive = sorted(c.signal_type for c in merged if c.weight >= min_weight)
    negative = sorted(c.signal_type for c in merged if -c.weight >= min_weight)
    if not positive or not negative:
        return ""
    return (
        "segnali contrastanti: "
        + " vs ".join(("+ " + ",".join(positive), "- " + ",".join(negative)))
    )


def _shortlist_sections(scored, min_signals, limit, single_min, single_limit):
    """Due sezioni tenute separate, non un unico ranking.

    **Convergenza multipla**: almeno `min_signals` moduli distinti. Il
    comportamento di prima: il dato e'incrociato da due fonti indipendenti.

    **Convinzione forte a fonte singola**: un solo modulo, ma un punteggio
    sopra `single_min`. Serve perche' `min_signals` escludeva i segnali piu'
    puri del run: nella finestra reale sei ticker a +45 (fra cui CLPR, un insider
    che compra il 100% della propria posizione) restavano fuori solo perche'
    nessuna seconda fonte li confermava. Non sono rumore, sono **non confermati**,
    che e' una informazione diversa.

    Le due liste non si mescolano: un +55 a fonte singola e un +35 a due moduli
    non sono confrontabili, e metterli in fila unica farebbe sembrare il primo
    piu' convincente del secondo. Ogni sezione ha il proprio tetto, cosi' la
    seconda non puo' sommergere la prima.

    L'ordinamento dentro ogni sezione e' per punteggio decrescente, poi
    copertura, poi ticker: senza i due tie-breaker due run identici potrebbero
    produrre shortlist diverse.
    """
    multi = [row for row in scored if row[1] >= min_signals]
    # `single_min = None` vuol dire sezione 2 DISABILITATA, cioe' il comportamento
    # di prima della modifica. Non `0`: con la soglia a zero ogni ticker a un
    # solo modulo entrerebbe in shortlist, e una chiave dimenticata in config
    # cambierebbe il risultato invece di lasciare le cose come erano.
    single = [
        row for row in scored
        if single_min is not None
        and row[1] < min_signals
        and row[0] >= single_min
    ]
    for sezione in (multi, single):
        sezione.sort(key=lambda r: (-r[0], -r[1], r[2]))
    return multi[:limit], single[:single_limit]