"""Fase 7a: generatore della dashboard statica.

Legge `signals` (righe di sintesi `composite` + contributi) e produce un
singolo file HTML autosufficiente (CSS inline, nessun JS, nessuna dipendenza
esterna): la pagina va servita da GitHub Pages cosi' com'e'.

Quattro scelte, ognuna legata a un difetto che si era visto nei dati reali:

1. **Read-only.** Si collega con `mode=ro`: la dashboard non puo' scrivere
   nel DB, e il subcomando CLI non chiama `db.init_schema` (che e' uno scrittore).
2. **Le due sezioni restano due tabelle.** Un +55 a fonte singola e un +35 a
   due moduli non sono confrontabili: metterli in un unico ranking li
   metterebbe in fila. La separazione gia' esiste nella description della riga
   di sintesi, quindi qui non si ricalcola nulla.
3. **I casi sporchi si gestiscono in rendering, non correggendo i dati.**
   `companies.name IS NULL` (18 company con segnali) rende `None` nel testo:
   esce come un trattino con un title che lo dice. Punteggi negativi (BLLN
   -12.0) hanno una colonna punteggio con segno e colore, e restano nella
   shortlist: e' un dato vero.
4. **Tutto quello che non entra in shortlist e' visibile, non nascosto.** I
   compositi fuori lista finiscono in un `<details>` con il motivo
   (fuori tetto / sotto soglia fonte singola), cosi' la pagina non promette
   una completezza che non ha.

La copertura e il voto vengono letti dalla description con regex: la riga di
sintesi e' scritta dal modulo di scoring apposta per questo (vedi
`modules/scoring/module.py`), e ricalcolare copertura o score dalla tabella
sarebbe duplicare la logica della Fase 6 nella Fase 7.
"""
from __future__ import annotations

import html
import re
import sqlite3
from pathlib import Path

# Etichetta importata dal modulo di scoring: e' la stessa stringa che la Fase 6
# scrive nella description, e qui serve per marcare la sezione 2 senza
# duplicare la regola (copertura < min_signals E score >= single_source_min).
from modules.scoring.module import SINGLE_SOURCE_LABEL

_COVERAGE_RE = re.compile(r"copertura (\d+)/(\d+) \(([^)]*)\)")
_CONTRIBS_RE = re.compile(r"da (\d+) contributi")
_CONFLICT_PREFIX = "segnali contrastanti"

#: Freschezza massima per fonte, letta dalle tabelle sorgente (non da
#: date('now')): la pagina deve dire quanto e' vecchio il dato che mostra, e
#: l'ancoraggio alle tabelle e' lo stesso principio della Fase 6.
_FRESHNESS = (
    ("Prezzi", "SELECT MAX(date) FROM price_snapshots"),
    ("Insider (Form 4)", "SELECT MAX(filing_date) FROM insider_transactions"),
    ("13F", "SELECT MAX(filing_date) FROM institutional_holdings"),
    ("Notizie", "SELECT MAX(published_at) FROM news_events"),
)

_SQL_COMPOSITE = """
    SELECT s.magnitude AS score, s.description AS description, s.generated_at,
           s.company_id, c.ticker, c.name
      FROM signals s
      JOIN companies c ON c.id = s.company_id
     WHERE s.module_key = 'scoring' AND s.signal_type = 'composite'
       AND s.signal_date = ?
     ORDER BY s.magnitude DESC, s.id
"""

_SQL_CONTRIB = """
    SELECT s.module_key, s.signal_type, s.magnitude, s.direction,
           s.description, s.signal_date, c.ticker, c.name
      FROM signals s
      JOIN companies c ON c.id = s.company_id
     WHERE s.signal_date = ? AND NOT (s.module_key = 'scoring'
                                      AND s.signal_type = 'composite')
     ORDER BY c.ticker, s.module_key, s.signal_type
"""

_MODULE_LABEL = {
    "insider_trading": "Insider",
    "price_screener": "Prezzi",
    "news_sentiment": "Notizie",
    "institutional_holdings": "13F",
    "scoring": "Scoring",
}


def connect_readonly(path) -> sqlite3.Connection:
    """Connessione in sola lettura: la dashboard non deve toccare il DB."""
    p = Path(path)
    conn = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def _e(text) -> str:
    """Escape di tutto quello che viene dal DB: ticker, nome, description.

    Le description contengono testo di notizie e nomi di gestori: senza escape
    una virgoletta o un `<` in una notizia chiuderebbe la cella e la pagina.
    """
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def _score_cell(value) -> str:
    """Cella punteggio con segno e colore: -12.0 resta visibile come negativo."""
    v = float(value)
    klass = "neg" if v < 0 else "pos"
    return f'<td class="num {klass}">{v:+.1f}</td>'


def _name_cell(name, ticker: str) -> str:
    """Nome azienda: 18 company reali hanno name IS NULL.

    Senza questo handler la cella esce come "None" (o vuota, a seconda del
    driver): il trattino con un title dice che il nome manca nel DB invece di
    sembrare un bug della pagina.
    """
    if name is None or str(name).strip() == "":
        return (
            f'<td class="muted" title="nome non disponibile '
            f'nel database per { _e(ticker) }">&mdash;</td>'
        )
    return f"<td>{_e(name)}</td>"


def _date_only(value) -> str:
    """Solo la data: le notizie hanno l'orario ISO, il resto solo la data."""
    if not value:
        return ""
    return str(value)[:10]


def parse_composite(description: str) -> dict:
    """Estrae dalla riga di sintesi copertura, contributi, contrasto, fonte singola.

    Il formato e' quello scritto da `modules/scoring/module.py`:
    `score +68.0 da 2 contributi, copertura 2/4 (a,b)[; segnali contrastanti:
    ...][; <SINGLE_SOURCE_LABEL>]`. Se il formato cambia la pagina degrada in
    modo visibile (copertura assente = colonna vuota), non produce numeri
    inventati: nessun valore viene dedotto dal resto della riga.
    """
    text = description or ""
    out = {"coverage": None, "coverage_total": None, "sources": "",
           "contribs": None, "conflict": "", "single_source": False}
    m = _COVERAGE_RE.search(text)
    if m:
        out["coverage"] = int(m.group(1))
        out["coverage_total"] = int(m.group(2))
        out["sources"] = m.group(3)
    m = _CONTRIBS_RE.search(text)
    if m:
        out["contribs"] = int(m.group(1))
    for chunk in text.split("; "):
        if chunk.startswith(_CONFLICT_PREFIX):
            out["conflict"] = chunk
    out["single_source"] = SINGLE_SOURCE_LABEL in text
    return out


def split_sections(rows, *, min_signals, shortlist_size,
                   single_source_limit, single_min):
    """Divide i compositi in due shortlist + il resto, con il motivo.

    Stessa regola e stesso ordinamento di `_shortlist_sections` nella Fase 6
    (punteggio decrescente, poi copertura, poi ticker) perche' la pagina deve
    mostrare le righe che il modulo ha giÃ  classificato, non una classifica
    diversa. `single_min is None` disabilita la sezione 2, come nel modulo.

    `rest` contiene tutto il resto della data con una `reason` leggibile: la
    shortlist e' un tetto, non un filtro sul valore del segnale.
    """
    def _key(r):
        return (-r["score"], -(r["coverage"] or 0), r["ticker"])

    # Prima si ordina POI si taglia: il tetto va applicato alla classifica
    # finale, non all'ordine con cui le righe escono dalla query.
    pool = sorted(rows, key=_key)
    multi_all = [r for r in pool if r["coverage"] is not None
                 and r["coverage"] >= min_signals]
    single_all = [
        r for r in pool
        if r["coverage"] is not None
        and r["coverage"] < min_signals
        and score_ok(r["score"], single_min)
    ]

    multi = multi_all[:shortlist_size]
    single = single_all[:single_source_limit]

    rest = []
    for row in pool:
        if row in multi or row in single:
            continue
        coverage, score = row["coverage"], row["score"]
        if coverage is None:
            reason = "descrizione non interpretabile"
        elif coverage >= min_signals:
            reason = f"fuori tetto sezione 1 ({shortlist_size})"
        elif single_min is None:
            reason = "fonte singola, sezione 2 disabilitata"
        elif score < single_min:
            reason = f"sotto soglia fonte singola ({single_min:+.0f})"
        else:
            reason = f"fuori tetto sezione 2 ({single_source_limit})"
        rest.append({**row, "reason": reason})
    return multi, single, rest


def score_ok(score: float, single_min) -> bool:
    """`single_min is None` = sezione 2 disabilitata, non soglia a zero."""
    return single_min is not None and score >= single_min

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 1.5rem; font: 14px/1.5 system-ui, -apple-system,
       "Segoe UI", sans-serif; color: #1b1f23; background: #fff; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1.1rem; margin: 2rem 0 .5rem; }
h2 .count { color: #57606a; font-weight: 400; font-size: .9rem; }
p.sub { margin: 0; color: #57606a; }
table { border-collapse: collapse; width: 100%; margin-bottom: .5rem; }
th, td { padding: .35rem .5rem; border-bottom: 1px solid #d8dee4; text-align: left;
         vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
                 white-space: nowrap; }
.pos { color: #1a7f37; font-weight: 600; }
.neg { color: #b42318; font-weight: 600; }
.muted { color: #8b949e; }
.badge { display: inline-block; padding: 0 .4rem; border-radius: .75rem;
         font-size: .78rem; border: 1px solid; }
.badge.conflict { color: #9a6700; border-color: #d4a72c; background: #fff8c5; }
.badge.single { color: #57606a; border-color: #d0d7de; background: #f6f8fa; }
.src { color: #57606a; font-size: .8rem; }
details { margin: .5rem 0 1rem; }
summary { cursor: pointer; color: #57606a; }
.desc { color: #57606a; font-size: .82rem; }
.fresh { display: flex; flex-wrap: wrap; gap: 1rem; margin: .75rem 0 0;
         padding: .5rem .75rem; background: #f6f8fa; border-radius: .5rem;
         font-size: .85rem; }
.fresh b { font-weight: 600; }
footer { margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid #d8dee4;
         color: #57606a; font-size: .82rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e6edf3; background: #0d1117; }
  th { background: #161b22; }
  td, th { border-color: #30363d; }
  .badge.single, .fresh, details summary { background: #161b22; color: #8b949e; }
  .muted, .desc, .src, footer, p.sub, h2 .count { color: #8b949e; }
  .badge.conflict { background: #221c07; color: #d4a72c; }
  .pos { color: #3fb950; } .neg { color: #f85149; }
  footer { border-color: #30363d; }
}
"""


def _freshness(conn) -> str:
    """Massimo per fonte: la pagina dichiara l'eta' dei dati che mostra."""
    cells = []
    for label, sql in _FRESHNESS:
        try:
            value = conn.execute(sql).fetchone()[0]
        except sqlite3.Error:
            value = None
        shown = _e(_date_only(value)) if value else "<span class=\"muted\">n/d</span>"
        cells.append(f"<span><b>{_e(label)}:</b> {shown}</span>")
    return '<div class="fresh">' + "".join(cells) + "</div>"


def _coverage_cell(info: dict, description: str = "") -> str:
    if info["coverage"] is None:
        return '<td class="muted">&mdash;</td>'
    sources = _e(info["sources"]).replace(",", ", ")
    # il title riporta la description intera della riga di sintesi: lo score
    # resta tracciabile senza cliccare nulla
    title = f' title="{_e(description)}"' if description else ""
    return (
        f'<td class="num"{title}>{info["coverage"]}/{info["coverage_total"]}</td>'
        f'<td class="src">{sources}</td>'
    )


def _conflict_cell(info: dict) -> str:
    if not info["conflict"]:
        return '<td></td>'
    label = info["conflict"].replace(_CONFLICT_PREFIX + ": ", "")
    return f'<td><span class="badge conflict" title="{_e(info["conflict"])}">{_e(label)}</span></td>'


def _shortlist_table(rows, *, section: int) -> str:
    """Tabella di una sezione. La 2 ha in piu' il badge fonte singola."""
    head = (
        "<tr><th class=\"num\">#</th><th>Ticker</th><th>Nome</th>"
        "<th class=\"num\">Punteggio</th><th class=\"num\">Cop.</th>"
        "<th>Fonti</th><th>Contrasto</th><th>Nota</th></tr>"
    )
    body = []
    for i, row in enumerate(rows, 1):
        info = parse_composite(row["description"])
        badge = ('<span class="badge single">fonte singola, non confermata</span>'
                 if section == 2 else "")
        body.append(
            "<tr>"
            f'<td class="num">{i}</td>'
            f'<td><b>{_e(row["ticker"])}</b></td>'
            + _name_cell(row["name"], row["ticker"])
+ _score_cell(row["score"])
            + _coverage_cell(info, row["description"])
            + _conflict_cell(info)
            + f"<td>{badge}</td>"
            "</tr>"
        )
    if not body:
        return '<p class="muted">Nessun ticker in questa sezione per la data selezionata.</p>'
    return f"<table>{head}{''.join(body)}</table>"


def _rest_details(rest) -> str:
    """Compositi fuori shortlist: visibili, con il motivo del mancato ingresso."""
    if not rest:
        return ""
    head = (
        "<tr><th>Ticker</th><th>Nome</th><th class=\"num\">Punteggio</th>"
        "<th class=\"num\">Cop.</th><th>Motivo</th></tr>"
    )
    body = []
    for row in rest:
        info = parse_composite(row["description"])
        coverage = (f'{info["coverage"]}/{info["coverage_total"]}'
                    if info["coverage"] is not None else "&mdash;")
        body.append(
            "<tr>"
            f'<td><b>{_e(row["ticker"])}</b></td>'
            + _name_cell(row["name"], row["ticker"])
            + _score_cell(row["score"])
            + f'<td class="num">{coverage}</td>'
            + f'<td class="muted">{_e(row.get("reason", ""))}</td>'
            "</tr>"
        )
    return (
        f"<details><summary>Altri segnali della data "
        f"({len(rest)}) fuori dalla shortlist</summary>"
        f"<table>{head}{''.join(body)}</table></details>"
    )

class NoSignals(Exception):
    """Nessuna riga di sintesi per la data richiesta: il CLI esce con codice 2."""


def _contrib_table(rows) -> str:
    """Tabella dei segnali grezzi: tutti i contributi della data, non solo
    quelli che sono entrati in shortlist."""
    head = (
        "<tr><th>Ticker</th><th>Nome</th><th>Fonte</th><th>Tipo</th>"
        "<th class=\"num\">Peso</th><th>Data</th><th>Descrizione</th></tr>"
    )
    body = []
    for row in rows:
        weight = float(row["magnitude"] or 0.0)
        klass = "neg" if weight < 0 else "pos"
        fonte = _MODULE_LABEL.get(row["module_key"], row["module_key"])
        body.append(
            "<tr>"
            f'<td><b>{_e(row["ticker"])}</b></td>'
            + _name_cell(row["name"], row["ticker"])
            + f"<td>{_e(fonte)}</td>"
            + f'<td class="src">{_e(row["signal_type"])}</td>'
            + f'<td class="num {klass}">{weight:+.1f}</td>'
            + f'<td class="muted">{_e(_date_only(row["signal_date"]))}</td>'
            + f'<td class="desc">{_e(row["description"])}</td>'
            "</tr>"
        )
    if not body:
        return '<p class="muted">Nessun contributo per la data selezionata.</p>'
    return f"<table>{head}{''.join(body)}</table>"


def build(conn, conf, *, signal_date=None) -> str:
    """Costruisce la pagina HTML per `signal_date` (default: ultima data)."""
    sconf = ((conf.get("modules") or {}).get("scoring") or {})
    min_signals = int(sconf.get("min_signals", 2))
    shortlist_size = int(sconf.get("shortlist_size", 25))
    single_limit = int(sconf.get("single_source_limit", 10))
    single_min = sconf.get("single_source_min")
    single_min = None if single_min is None else float(single_min)

    if signal_date is None:
        row = conn.execute(
            "SELECT MAX(signal_date) FROM signals"
            " WHERE module_key='scoring' AND signal_type='composite'"
        ).fetchone()
        signal_date = row[0] if row else None
    if not signal_date:
        raise NoSignals("nessuna riga di sintesi nel database")

    composites = conn.execute(_SQL_COMPOSITE, (signal_date,)).fetchall()
    if not composites:
        raise NoSignals(f"nessun segnale per {signal_date}")
    generated_at = max(r["generated_at"] for r in composites)

    rows = []
    for r in composites:
        info = parse_composite(r["description"])
        rows.append({
            "score": float(r["score"]), "coverage": info["coverage"],
            "ticker": r["ticker"], "name": r["name"],
            "description": r["description"], "info": info,
        })
    multi, single, rest = split_sections(
        rows, min_signals=min_signals, shortlist_size=shortlist_size,
        single_source_limit=single_limit, single_min=single_min,
    )
    contrib = conn.execute(_SQL_CONTRIB, (signal_date,)).fetchall()

    secs = [
        (1, "Convergenza multipla", multi,
         f"almeno {min_signals} moduli distinti d'accordo sullo stesso ticker"),
        (2, "Convinzione forte a fonte singola", single,
         "un solo modulo, ma un punteggio alto: nessuna seconda fonte lo conferma"),
    ]
    out = [
        "<!doctype html>",
        '<html lang="it"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Segnali di trading {_e(signal_date)}</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>Segnali di trading</h1>",
        f'<p class="sub">Data segnale <b>{_e(signal_date)}</b> &middot; '
        f"generato il {_e(generated_at)} &middot; "
        f"{len(multi) + len(single)} in shortlist su "
        f"{len(rows)} segnali compositi &middot; {len(contrib)} contributi grezzi</p>",
        _freshness(conn),
    ]
    for number, title, rows_sec, subtitle in secs:
        out.append(
            f"<h2>Sezione {number} &mdash; {_e(title)} "
            f'<span class="count">({len(rows_sec)})</span></h2>'
            f'<p class="sub">{_e(subtitle)}</p>'
            + _shortlist_table(rows_sec, section=number)
        )
    out.append(_rest_details(rest))
    out.append(
        f'<h2>Segnali grezzi <span class="count">({len(contrib)})</span></h2>'
        '<p class="sub">Tutti i contributi scritti dai 4 moduli per questa data: '
        'la shortlist sopra e\' una selezione di queste righe.</p>'
        + _contrib_table(contrib)
    )
    out.append(
        "<footer>Generato da <code>python -m core.cli dashboard</code> "
        "dal database versionato nel repo. Screening a supporto delle "
        "decisioni, non un consiglio d'investimento: i dati arrivano da "
        "fonti pubbliche (SEC EDGAR, borse, feed RSS) e possono essere "
        "incompleti o in ritardo.</footer>"
        "</body></html>"
    )
    return "".join(out)


def generate(db_path, conf, out_path, *, signal_date=None) -> Path:
    """Legge il DB in sola lettura e scrive il file HTML."""
    conn = connect_readonly(db_path)
    try:
        page = build(conn, conf, signal_date=signal_date)
    finally:
        conn.close()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
