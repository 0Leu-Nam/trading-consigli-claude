"""Test end-to-end del modulo institutional_holdings su DB temporaneo (rete mockata)."""

import json
from datetime import date

import pytest

from core import db
from core.module_interface import RunContext
from modules.institutional_holdings import cusip_map, edgar_13f
from modules.institutional_holdings import module as module_mod
from modules.institutional_holdings.module import Module, target_quarters

TABLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable>
  <infoTable>
    <nameOfIssuer>ABBVIE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>00287Y109</cusip><value>571474</value>
    <shrsOrPrnAmt><sshPrnamt>2271</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>1540000</value>
    <shrsOrPrnAmt><sshPrnamt>9000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>5000</value>
    <shrsOrPrnAmt><sshPrnamt>30</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>CALL</putCall>
  </infoTable>
  <infoTable>
    <nameOfIssuer>ACME WIDGETS LTD</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>XXXXXX999</cusip><value>1000</value>
    <shrsOrPrnAmt><sshPrnamt>50</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>
"""

TICKER_MAP_PAYLOAD = {
    "0": {"cik_str": 1551152, "ticker": "ABBV", "title": "AbbVie Inc."},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}

DEFAULT_CONFIG = {
    "lag_days": 45,
    "quarters_back": 1,
    "max_filings": 120,
    "max_filings_scan": 2000,
    "skip_put_call": True,
    "cusip_ttl_days": 60,
    "filer_cik_filter": [],
}


class _Resp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content
        self.text = content.decode(errors="ignore")

    def json(self):
        if self._payload is None:
            raise ValueError("non json")
        return self._payload


def _filing(accession="0000000001-26-000001", filer_cik="0000000001", name="ACME CAPITAL"):
    return edgar_13f.Filing13F(
        accession=accession, file_date="2026-08-01", period_ending="2026-06-30",
        ciks=[filer_cik], display_names=[name],
    )


@pytest.fixture()
def ctx_and_conn(tmp_path):
    path = tmp_path / "app.db"
    db.init_schema(path)
    conn = db.connect(path)
    ctx = RunContext(conn=conn, module_config=dict(DEFAULT_CONFIG), global_config={},
                     env_getter=lambda k, d=None: d)
    yield ctx, conn, path
    conn.close()


def _mock_all(monkeypatch, filings=None):
    monkeypatch.setattr(edgar_13f, "search_13f", lambda *a, **k: list(filings or [_filing()]))
    monkeypatch.setattr(edgar_13f, "discover_information_table", lambda *a, **k: "https://x/info.xml")
    monkeypatch.setattr(edgar_13f, "_get", lambda *a, **k: _Resp(content=TABLE_XML))
    monkeypatch.setattr(cusip_map, "_get", lambda *a, **k: _Resp(payload=TICKER_MAP_PAYLOAD))
    monkeypatch.setattr(cusip_map, "_MAP_TTL_SECONDS", 0)


def test_inserts_resolved_rows_and_reports_skips(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)
    result = Module().run(ctx)
    conn.commit()

    assert result.status == "ok"
    assert result.rows_written == 2  # ABBVIE + APPLE (CALL scartata, ACME non risolto)
    assert "cusip non risolti=1" in result.note
    assert "opzioni scartate=1" in result.note
    assert result.watermark == date.today().isoformat()

    quarter = target_quarters(date.today(), 45, 1)[0].quarter
    row = conn.execute(
        """SELECT filing_quarter, filing_date, filer_name, filer_cik, issuer_name,
                  cusip, shares, value_usd, shares_delta
           FROM institutional_holdings WHERE cusip = '00287Y109'"""
    ).fetchone()
    assert row["filing_quarter"] == quarter
    assert row["filing_date"] == "2026-08-01"
    assert row["filer_name"] == "ACME CAPITAL"
    assert row["filer_cik"] == "0000000001"
    assert row["issuer_name"] == "ABBVIE INC"
    assert row["shares"] == 2271
    assert row["value_usd"] == 571474  # grezzo
    assert row["shares_delta"] is None  # primo trimestre osservato


def test_unresolved_cusip_is_tracked_but_not_inserted(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)
    Module().run(ctx)
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM institutional_holdings WHERE cusip = 'XXXXXX999'"
    ).fetchone()["n"] == 0
    tracked = conn.execute(
        "SELECT company_id, resolved_at FROM cusip_lookup WHERE cusip = 'XXXXXX999'"
    ).fetchone()
    assert tracked["company_id"] is None
    assert tracked["resolved_at"] is None


def test_second_run_is_idempotent(ctx_and_conn, monkeypatch):
    ctx, conn, path = ctx_and_conn
    _mock_all(monkeypatch)
    first = Module().run(ctx)
    conn.commit()
    second = Module().run(ctx)
    conn.commit()
    assert first.rows_written == 2
    assert second.rows_written == 0
    with db.connect(path) as check:
        assert check.execute("SELECT COUNT(*) AS n FROM institutional_holdings").fetchone()["n"] == 2


def test_partial_error_does_not_fail_the_run(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    good = _filing(accession="0000000001-26-000001", filer_cik="0000000001")
    bad = _filing(accession="0000000002-26-000002", filer_cik="0000000002", name="KO CAPITAL")
    _mock_all(monkeypatch, filings=[good, bad])

    def fake_discover(_ua, accession, _ciks):
        if accession == bad.accession:
            raise edgar_13f.SecEdgarError("timeout")
        return "https://x/info.xml"

    monkeypatch.setattr(edgar_13f, "discover_information_table", fake_discover)
    result = Module().run(ctx)
    conn.commit()
    assert result.status == "ok"
    assert result.rows_written == 2              # il filing buono ha prodotto i suoi 2 titoli
    assert len(result.errors) == 1
    assert bad.accession in result.errors[0]
    assert "errori parziali=1" in result.note


def test_search_failure_is_a_hard_error(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)

    def boom(*a, **k):
        raise edgar_13f.SecEdgarError("EFTS non raggiungibile")

    monkeypatch.setattr(edgar_13f, "search_13f", boom)
    result = Module().run(ctx)
    assert result.status == "error"
    assert result.rows_written == 0
    assert "ricerca EFTS fallita" in result.errors[0]


def test_whitelisted_filer_takes_the_slot_over_an_earlier_efts_hit(ctx_and_conn, monkeypatch):
    """Whitelist esclusiva con budget esaurito: il CIK in lista prende l'unico slot
    anche se EFTS l'ha restituito DOPI un filer estraneo, che viene scartato.

    Un tempo questo test verificava una coda di priorita' ("whitelist first, poi
    gli altri se restano slot"). Quella logica non esiste piu': con whitelist
    attiva gli estranei non entrano affatto, quindi il confronto diretto con
    l'ordine di EFTS non ha senso. Resta pero' il caso peggiore da coprire,
    cioe' il tetto che potrebbe far vincere l'estraneo perche' arriva prima."""
    ctx, conn, _path = ctx_and_conn
    other = _filing(accession="0000000009-26-000009", filer_cik="0000000009", name="OTHER FUND")
    wanted = _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND")
    _mock_all(monkeypatch, filings=[other, wanted])
    ctx.module_config["max_filings"] = 1
    ctx.module_config["filer_cik_filter"] = [123]  # CIK breve: normalizzato a 10 cifre

    result = Module().run(ctx)
    conn.commit()
    filer = conn.execute("SELECT DISTINCT filer_cik FROM institutional_holdings").fetchone()
    assert filer["filer_cik"] == "0000000123"
    assert "cik_whitelist=1/1" in result.note
    assert result.errors == []
    assert result.rows_written == 2  # budget 1 filing → 2 titoli risolti


# ── il filtro della whitelist va messo lato RICERCA (bug del run #16) ─────────
# L'ordine di EFTS non e' quello del deposito cercato: i gestori richiesti
# stavano alle posizioni 1132/1857/2052 su ~4000 accessions della finestra Q2,
# quindi la scansione limitata non li vedeva mai e il modulo scriveva le
# posizioni di fund mai richiesti, con status 'ok' e nessun errore.

def test_whitelist_ciks_are_passed_to_the_search(ctx_and_conn, monkeypatch):
    """Il CIK deve arrivare a EFTS: altrimenti il modulo legge il primo N e ignora
    la whitelist (confermato dal run #16 in produzione)."""
    ctx, _conn, _path = ctx_and_conn
    ctx.module_config["filer_cik_filter"] = ["0000000123", "0000000456"]
    seen: list[list[str]] = []

    def _capture(ua, start, end, cap, ciks=None):
        seen.append(list(ciks or []))
        return []

    monkeypatch.setattr(edgar_13f, "search_13f", _capture)
    monkeypatch.setattr(edgar_13f, "discover_information_table", lambda *a, **k: "https://x/info.xml")
    Module().run(ctx)
    assert seen, "nessuna finestra processata"
    assert all(c == ["0000000123", "0000000456"] for c in seen), seen


def test_no_ciks_param_when_whitelist_is_empty(ctx_and_conn, monkeypatch):
    """Senza whitelist il percorso resta quello generico 'primi N più recenti'."""
    ctx, _conn, _path = ctx_and_conn
    ctx.module_config["filer_cik_filter"] = []
    seen: list = []

    def _capture(ua, start, end, cap, ciks=None):
        seen.append(ciks)
        return []

    monkeypatch.setattr(edgar_13f, "search_13f", _capture)
    Module().run(ctx)
    assert seen and all(c is None for c in seen)


def test_filers_outside_whitelist_are_never_written(ctx_and_conn, monkeypatch):
    """Regressione diretta del run #16: la ricerca (o un suo mock) può restituire
    filer fuori whitelist; il modulo non deve scriverne le posizioni."""
    ctx, conn, _path = ctx_and_conn
    others = [
        _filing(accession="0000000009-26-000009", filer_cik="0000000009", name="NWM ADVISORS, LLC"),
        _filing(accession="0000000010-26-000010", filer_cik="0000000010", name="KRANE FINANCIAL"),
    ]
    wanted = _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND")
    # la ricerca restituisce TUTTI (comportamento del vecchio EFTS non filtrato)
    _mock_all(monkeypatch, filings=others + [wanted])
    ctx.module_config["filer_cik_filter"] = ["0000000123"]
    ctx.module_config["max_filings"] = 10

    result = Module().run(ctx)
    conn.commit()
    filers = {r["filer_cik"] for r in conn.execute("SELECT DISTINCT filer_cik FROM institutional_holdings")}
    assert filers == {"0000000123"}, f"filer scritti fuori whitelist: {filers}"
    assert "NWM ADVISORS, LLC" not in {r["filer_name"] for r in conn.execute("SELECT filer_name FROM institutional_holdings")}
    assert result.status == "ok"


def test_whitelist_configured_but_nothing_found_raises_warning(ctx_and_conn, monkeypatch):
    """Whitelist configurata e zero CIK trovati: il run non puo' restare un 'ok'
    silenzioso, altrimenti si crede di avere i 13F dei gestori scelti."""
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch, filings=[])          # ricerca che non trova nulla
    ctx.module_config["filer_cik_filter"] = ["0000000123", "0000000456"]

    result = Module().run(ctx)
    conn.commit()
    assert result.rows_written == 0
    assert result.errors, "nessun errore: il fallback silenzioso resta invisibile"
    assert "0000000123" in result.errors[0] and "0000000456" in result.errors[0]
    # il modulo resta 'ok' (non e' un fallimento tecnico) ma classify_run
    # deve classificare il run come 'warning'
    assert result.status == "ok"
    from core.orchestrator import classify_run
    assert classify_run([result], {"run": {"partial_error_threshold": 0}}) == "warning"


def test_partial_whitelist_hit_is_not_an_error(ctx_and_conn, monkeypatch):
    """Uno solo dei due gestori trovati è normale (l'altro può non aver depositato):
    non deve generare warning."""
    ctx, _conn, _path = ctx_and_conn
    found = _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND")
    _mock_all(monkeypatch, filings=[found])
    ctx.module_config["filer_cik_filter"] = ["0000000123", "0000000456"]

    result = Module().run(ctx)
    assert result.errors == []
    from core.orchestrator import classify_run
    assert classify_run([result], {"run": {"partial_error_threshold": 0}}) == "ok"
    assert "cik_whitelist=1/2" in result.note


def test_whitelist_with_fewer_matches_than_max_filings_never_adds_outsiders(tmp_path, monkeypatch):
    """Requisito strutturale: quando la whitelist è attiva i match sono MENO di
    max_filings, quindi ci sono slot liberi. Nonostante isso, per QUALUNQUE valore
    di max_filings non deve mai entrare un filer fuori whitelist: gli slot vuoti
    restano vuoti. Il parametro non viene più derivato dalla lunghezza della
    whitelist, quindi non deve poterla corrompere."""
    for max_filings in (1, 2, 3, 5, 6, 10, 120):
        # un DB pulito per ogni valore: il modulo riusa le righe di un run precedente
        # e falserebbe l'asserzione sui filer scritti
        path = tmp_path / f"app_{max_filings}.db"
        db.init_schema(path)
        conn = db.connect(path)
        ctx = RunContext(conn=conn, module_config=dict(DEFAULT_CONFIG), global_config={},
                         env_getter=lambda k, d=None: d)
        others = [
            _filing(accession="0000000009-26-000009", filer_cik="0000000009", name="NWM ADVISORS, LLC"),
            _filing(accession="0000000010-26-000010", filer_cik="0000000010", name="KRANE FINANCIAL"),
        ]
        wanted = _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND")
        # la ricerca "filtrata" restituisce anche estranei: il modulo deve comunque
        # tenere la whitelist esclusiva, per qualsiasi max_filings
        _mock_all(monkeypatch, filings=others + [wanted])
        ctx.module_config["filer_cik_filter"] = ["0000000123"]
        ctx.module_config["max_filings"] = max_filings

        result = Module().run(ctx)
        conn.commit()

        filers = {
            r["filer_cik"]
            for r in conn.execute("SELECT DISTINCT filer_cik FROM institutional_holdings")
        }
        assert filers == {"0000000123"}, (
            f"max_filings={max_filings}: filer scritti fuori whitelist: {filers}"
        )
        names = {
            r["filer_name"]
            for r in conn.execute("SELECT filer_name FROM institutional_holdings")
        }
        assert "NWM ADVISORS, LLC" not in names and "KRANE FINANCIAL" not in names, (
            f"max_filings={max_filings}: fund estranei scritti: {names}"
        )
        assert result.rows_written > 0, f"max_filings={max_filings}: nulla scritto"
        conn.close()


def test_generic_search_is_not_called_by_default(ctx_and_conn, monkeypatch):
    """Il fallback generico è opt-in: di default la whitelist esclusiva non deve
    mai far partire una ricerca EFTS senza il parametro `ciks`."""
    ctx, _conn, _path = ctx_and_conn
    seen: list = []

    def _capture(ua, start, end, cap, ciks=None):
        seen.append(ciks)
        return [_filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND")]

    monkeypatch.setattr(edgar_13f, "search_13f", _capture)
    ctx.module_config["filer_cik_filter"] = ["0000000123"]
    ctx.module_config["max_filings"] = 6
    ctx.module_config.pop("fill_remaining_with_generic", None)

    Module().run(ctx)
    assert seen, "nessuna finestra processata"
    assert all(c == ["0000000123"] for c in seen), f"ricerca generica non richiesta: {seen}"


def test_fill_remaining_with_generic_adds_outsiders_on_request(ctx_and_conn, monkeypatch):
    """Se il fallback è richiesto esplicitamente, gli slot residui si riempiono
    con filer generici e la nota dichiara il numero, così non è invisibile."""
    ctx, conn, _path = ctx_and_conn
    calls: list = []
    wanted = _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND")
    outsider = _filing(accession="0000000010-26-000010", filer_cik="0000000010", name="KRANE FINANCIAL")

    def _search(ua, start, end, cap, ciks=None):
        calls.append(ciks)
        if ciks:                      # ricerca filtrata: solo il gestore scelto
            return [wanted]
        return [outsider, wanted]     # ricerca generica: c'è anche un estraneo

    _mock_all(monkeypatch, filings=[])          #Discovery, _get e CUSIP già mockati
    monkeypatch.setattr(edgar_13f, "search_13f", _search)
    ctx.module_config["filer_cik_filter"] = ["0000000123"]
    ctx.module_config["max_filings"] = 6
    ctx.module_config["fill_remaining_with_generic"] = True

    result = Module().run(ctx)
    conn.commit()

    assert None in calls, "il fallback richiesto non ha mai cercato in modo generico"
    names = {
        r["filer_name"]
        for r in conn.execute("SELECT filer_name FROM institutional_holdings")
    }
    assert "KRANE FINANCIAL" in names, "il fallback esplicito non ha riempito lo slot"
    assert "filer generici=1" in result.note, result.note


def test_fill_remaining_with_generic_ignored_without_whitelist(ctx_and_conn, monkeypatch):
    """Senza whitelist il modulo è già generico: il flag non deve cambiare nulla."""
    ctx, _conn, _path = ctx_and_conn
    seen: list = []

    def _capture(ua, start, end, cap, ciks=None):
        seen.append(ciks)
        return []

    monkeypatch.setattr(edgar_13f, "search_13f", _capture)
    ctx.module_config["filer_cik_filter"] = []
    ctx.module_config["fill_remaining_with_generic"] = True

    result = Module().run(ctx)
    assert seen and all(c is None for c in seen), seen
    assert result.errors == []


def test_cap_dropping_a_whitelisted_filer_is_reported(ctx_and_conn, monkeypatch):
    """Il cap può esaurire il budget e far perdere il 13F di un gestore richiesto:
    è il secondo fallizio silenzioso (diverso dai filer estranei del run #16).
    Se il CIK è stato trovato ma non processato, va segnalato."""
    ctx, _conn, _path = ctx_and_conn
    filings = [
        _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND"),
        _filing(accession="0000000456-26-000456", filer_cik="0000000456", name="OTHER FUND"),
    ]
    _mock_all(monkeypatch, filings=filings)
    ctx.module_config["filer_cik_filter"] = ["0000000123", "0000000456"]
    ctx.module_config["max_filings"] = 1

    result = Module().run(ctx)
    assert result.errors, "cap che scarta un gestore whitelistato: nessun errore"
    assert any("0000000456" in err and "max_filings=1" in err for err in result.errors), result.errors
    from core.orchestrator import classify_run
    assert classify_run([result], {"run": {"partial_error_threshold": 0}}) == "warning"


def test_cap_dropping_only_an_amendment_is_not_an_error(ctx_and_conn, monkeypatch):
    """Se il CIK resta coperto da un altro suo filing, il cap non ha perso nulla
    di richiesto: un 13F-HR/A in più è normale e non deve generare warning."""
    ctx, _conn, _path = ctx_and_conn
    filings = [
        _filing(accession="0000000123-26-000123", filer_cik="0000000123", name="WANTED FUND"),
        _filing(accession="0000000123-26-000124", filer_cik="0000000123", name="WANTED FUND"),
    ]
    _mock_all(monkeypatch, filings=filings)
    ctx.module_config["filer_cik_filter"] = ["0000000123"]
    ctx.module_config["max_filings"] = 1

    result = Module().run(ctx)
    assert result.errors == [], result.errors
    from core.orchestrator import classify_run
    assert classify_run([result], {"run": {"partial_error_threshold": 0}}) == "ok"


def test_empty_result_without_whitelist_is_not_an_error(ctx_and_conn, monkeypatch):
    """Senza whitelist non c'è nulla da segnalare: è il caso normale."""
    ctx, _conn, _path = ctx_and_conn
    _mock_all(monkeypatch, filings=[])
    ctx.module_config["filer_cik_filter"] = []
    result = Module().run(ctx)
    assert result.errors == []


def test_shares_delta_is_computed_against_previous_quarter(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    windows = target_quarters(date.today(), 45, 2)
    company_id = db.upsert_company(conn, "ABBV", name="ABBVIE INC", cik="0001551152")
    conn.execute(
        """INSERT INTO institutional_holdings
             (company_id, filing_quarter, filer_cik, cusip, issuer_name, shares, value_usd)
           VALUES (?, ?, '0000000001', '00287Y109', 'ABBVIE INC', 2000, 400000)""",
        (company_id, windows[1].quarter),
    )
    conn.commit()

    _mock_all(monkeypatch)
    Module().run(ctx)
    conn.commit()
    row = conn.execute(
        "SELECT shares_delta FROM institutional_holdings WHERE cusip = '00287Y109' AND filing_quarter = ?",
        (windows[0].quarter,),
    ).fetchone()
    assert row["shares_delta"] == 271  # 2271 - 2000


def test_put_call_rows_can_be_kept_if_configured(ctx_and_conn, monkeypatch):
    ctx, conn, _path = ctx_and_conn
    ctx.module_config["skip_put_call"] = False
    _mock_all(monkeypatch)
    result = Module().run(ctx)
    conn.commit()
    # La CALL su APPLE ha lo stesso CUSIP della posizione azionaria: la UNIQUE
    # (filing_quarter, filer_cik, cusip) la ignora, quindi resta 2 righe.
    assert result.rows_written == 2
    assert "opzioni scartate=0" in result.note


def test_principal_amount_rows_are_skipped(ctx_and_conn, monkeypatch):
    # Obbligazioni/fondi (sshPrnamtType=PRN) non sono posizioni azionarie.
    ctx, conn, _path = ctx_and_conn
    bond_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <informationTable>
      <infoTable>
        <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>DEBT</titleOfClass>
        <cusip>037833100</cusip><value>999</value>
        <shrsOrPrnAmt><sshPrnamt>777</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
      <infoTable>
        <nameOfIssuer>ABBVIE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
        <cusip>00287Y109</cusip><value>571474</value>
        <shrsOrPrnAmt><sshPrnamt>2271</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
    </informationTable>"""
    _mock_all(monkeypatch)
    monkeypatch.setattr(edgar_13f, "_get", lambda *a, **k: _Resp(content=bond_xml))

    result = Module().run(ctx)
    conn.commit()
    assert "non-SH scartate=1" in result.note
    assert result.rows_written == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM institutional_holdings WHERE cusip = '037833100'"
    ).fetchone()["n"] == 0


def test_filing_with_foreign_period_is_skipped(ctx_and_conn, monkeypatch):
    # Caso reale osservato: un 13F depositato ad agosto con period_ending
    # 2024-12-31. Non deve essere etichettato come 2026Q2.
    ctx, conn, _path = ctx_and_conn
    stale = _filing(accession="0000000007-26-000007", filer_cik="0000000007",
                     name="STALE FUND")
    stale.period_ending = "2024-12-31"
    _mock_all(monkeypatch, filings=[stale])

    result = Module().run(ctx)
    conn.commit()
    assert result.rows_written == 0
    assert "periodo fuori finestra=1" in result.note
    assert conn.execute("SELECT COUNT(*) AS n FROM institutional_holdings").fetchone()["n"] == 0
    # Il CUSIP non viene nemmeno messo in cache: il filing è ignorato intero.
    assert conn.execute("SELECT COUNT(*) AS n FROM cusip_lookup").fetchone()["n"] == 0


def test_period_matches_quarter_helper():
    assert module_mod.period_matches_quarter("2026-06-30", "2026Q2") is True
    assert module_mod.period_matches_quarter("2026-03-31", "2026Q1") is True
    assert module_mod.period_matches_quarter("2024-12-31", "2026Q2") is False
    assert module_mod.period_matches_quarter("", "2026Q2") is True          # ignoto → accetta
    assert module_mod.period_matches_quarter("non-una-data", "2026Q2") is True


def test_unexpected_row_error_does_not_fail_the_filing(ctx_and_conn, monkeypatch):
    # Un errore non previsto sul singolo CUSIP (es. rete/DB) non deve far
    # fallire il run: va in errors[] e gli altri titoli procedono.
    ctx, conn, _path = ctx_and_conn
    _mock_all(monkeypatch)
    calls = {"n": 0}
    real_resolve = cusip_map.resolve

    def flaky_resolve(user_agent, connection, cusip, issuer_name, **kwargs):
        calls["n"] += 1
        if cusip == "00287Y109":
            raise RuntimeError("boom nel resolver")
        return real_resolve(user_agent, connection, cusip, issuer_name, **kwargs)

    monkeypatch.setattr(cusip_map, "resolve", flaky_resolve)
    result = Module().run(ctx)
    conn.commit()
    assert result.status == "ok"
    assert result.rows_written == 1          # solo APPLE, ABBVIE è fallito
    assert any("boom nel resolver" in err for err in result.errors)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM institutional_holdings WHERE cusip = '00287Y109'"
    ).fetchone()["n"] == 0