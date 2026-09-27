"""Test di registry (scoperta plugin), modulo_interface e orchestratore."""

from core import db
from core.module_interface import ModuleInterface, RunContext, ModuleResult
from core.registry import discover_module_classes
from core.orchestrator import classify_run, run_all


class _SampleModule(ModuleInterface):
    key = "sample"
    display_name = "Sample"

    def run(self, ctx):
        return ModuleResult(module_key=self.key, rows_written=1, watermark="2026-09-20", note="ok")


class _PartialErrorModule(ModuleInterface):
    key = "sample"
    display_name = "Sample"

    def run(self, ctx):
        return ModuleResult(
            module_key=self.key,
            rows_written=1,
            watermark="2026-09-20",
            note="filing esaminati=250",
            errors=["filing 000-1: documento ownership non trovato"],
        )


def test_discover_registers_insider_trading():
    classes = discover_module_classes()
    assert "insider_trading" in classes
    mod = classes["insider_trading"]()
    assert isinstance(mod, ModuleInterface)
    assert mod.key == "insider_trading"


def test_run_context_get_env_and_config():
    ctx = RunContext(conn=None, module_config={"a": 1}, global_config={}, env_getter=lambda k, d="x": "y" if k == "K" else d)
    assert ctx.get("a") == 1
    assert ctx.get("missing", "z") == "z"
    assert ctx.env("K") == "y"
    assert ctx.db is None


def test_run_all_writes_run_log_and_state(tmp_path, monkeypatch):
    monkeypatch.setattr("core.registry.create_module", lambda key: _SampleModule())
    config = {
        "database": {"path": str(tmp_path / "app.db")},
        "modules": {"sample": {"enabled": True}},
    }
    results, run_id = run_all(config=config)
    assert len(results) == 1
    assert results[0].status == "ok"

    with db.connect(config["database"]["path"]) as conn:
        run = conn.execute("SELECT * FROM run_log WHERE id = ?", (run_id,)).fetchone()
        assert run["status"] == "ok"
        assert run["modules_run"] == '["sample"]'
        state = db.get_module_state(conn, "sample")
        assert state["last_processed_id"] == "2026-09-20"
        assert state["last_run_at"] is not None


def test_classify_run_ok():
    results = [ModuleResult(module_key="a", status="ok"), ModuleResult(module_key="b", status="skipped")]
    assert classify_run(results) == "ok"
    assert classify_run(results, {"run": {"partial_error_threshold": 0}}) == "ok"


def test_classify_run_warning_on_partial_errors():
    results = [ModuleResult(module_key="a", status="ok", errors=["filing x non trovato"])]
    assert classify_run(results) == "warning"
    assert classify_run(results, {"run": {"partial_error_threshold": 0}}) == "warning"


def test_classify_run_threshold_allows_some_partial_errors():
    results = [ModuleResult(module_key="a", status="ok", errors=["filing x non trovato"])]
    assert classify_run(results, {"run": {"partial_error_threshold": 1}}) == "ok"


def test_classify_run_error_on_module_failure():
    results = [ModuleResult(module_key="a", status="ok", errors=["parziale"]), ModuleResult(module_key="b", status="error", errors=["boom"])]
    assert classify_run(results) == "error"


def test_run_all_partial_error_is_warning_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr("core.registry.create_module", lambda key: _PartialErrorModule())
    config = {
        "database": {"path": str(tmp_path / "app.db")},
        "modules": {"sample": {"enabled": True}},
    }
    results, run_id = run_all(config=config)
    assert results[0].status == "ok"  # errore parziale NON degrada il modulo

    with db.connect(config["database"]["path"]) as conn:
        run = conn.execute("SELECT * FROM run_log WHERE id = ?", (run_id,)).fetchone()
        assert run["status"] == "warning"
        assert "documento ownership non trovato" in run["errors"]