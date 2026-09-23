"""Test del modulo price_screener su DB temporaneo (rete mockata).

La rete è simulata a livello di ``data_sources.fetch_history`` (provider), così
il modulo viene testato end-to-end senza toccare Yahoo/Stooq.
"""

from core import db
from core.module_interface import RunContext
from modules.price_screener import data_sources
from modules.price_screener import module as pricer
from modules.price_screener.data_sources import Bar


def _bars(n=30, close_start=100.0):
    return [Bar(date=f"2026-08-%02d" % (i + 1), open=100.0, high=101.0, low=99.0, close=close_start + i, volume=1_000_000) for i in range(n)]


def _ctx(tmp_path, module_config):
    path = tmp_path / "app.db"
    db.init_schema(path)
    conn = db.connect(path)
    return RunContext(conn=conn, module_config=module_config, global_config={}, env_getter=lambda k, d=None: d), conn, str(path)


def test_module_inserts_idempotent_and_rolls_back_snapshots(tmp_path, monkeypatch):
    bars_map = {"AAPL": _bars(30), "MSFT": _bars(30), "DEAD": []}
    monkeypatch.setattr(data_sources, "fetch_history", lambda t, days=30, stooq_key=None: (bars_map, ["DEAD"]))

    ctx, conn, path = _ctx(tmp_path, {"period_days": 30, "history_days": 25, "max_symbols": 500, "universe": ["AAPL", "MSFT", "DEAD"]})
    r1 = pricer.Module().run(ctx)
    conn.commit()
    assert r1.status == "ok"
    assert r1.rows_written == 2 * 25
    assert "senza dati=1" in r1.note
    assert r1.watermark == "2026-08-30"

    conn2 = db.connect(path)
    assert conn2.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"] == 2
    assert conn2.execute("SELECT COUNT(*) AS n FROM price_snapshots").fetchone()["n"] == 50
    conn2.close()

    r2 = pricer.Module().run(ctx)
    conn.commit()
    assert r2.rows_written == 0  # date già presenti: INSERT OR IGNORE

    full = db.connect(path)
    assert full.execute("SELECT COUNT(*) AS n FROM price_snapshots").fetchone()["n"] == 50
    full.close()
    conn.close()


def test_module_error_propagates_from_primary_source(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise data_sources.PriceSourceError("yfinance giù")

    monkeypatch.setattr(data_sources, "fetch_history", _boom)
    ctx, conn, path = _ctx(tmp_path, {"universe": ["AAPL"]})
    r = pricer.Module().run(ctx)
    assert r.status == "error"
    assert "yfinance giù" in r.errors[0]
    conn.close()


def test_module_invalid_universe_returns_error(tmp_path):
    ctx, conn, path = _ctx(tmp_path, {"universe": "sp9999"})
    r = pricer.Module().run(ctx)
    assert r.status == "error"
    conn.close()


def test_module_empty_universe_is_skipped(tmp_path):
    ctx, conn, path = _ctx(tmp_path, {"universe": []})
    r = pricer.Module().run(ctx)
    assert r.status == "skipped"
    conn.close()


def test_resolve_universe_file_sp500():
    tickers = pricer.resolve_universe({"universe": "file:data/sp500.txt"})
    assert len(tickers) > 400
    assert tickers == sorted(tickers)
    assert "AAPL" in tickers


def test_max_symbols_cap_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(data_sources, "fetch_history", lambda t, days=30, stooq_key=None: ({t: _bars(30) for t in t}, []))
    ctx, conn, path = _ctx(tmp_path, {"history_days": 3, "max_symbols": 2, "universe": ["A", "B", "C"]})
    r = pricer.Module().run(ctx)
    assert r.rows_written == 2 * 3  # solo A e B, C esclusa dal cap
    conn.close()


def test_universe_uppercase_and_skip_comments(tmp_path):
    cfg = {"universe": [" aapl ", "", "MSFT", "# not-a-ticker"]}
    tickers = pricer.resolve_universe(cfg)
    assert tickers == ["AAPL", "MSFT"]