"""Test di registry (scoperta plugin), modulo_interface e orchestratore."""

from core import db
from core.module_interface import ModuleInterface, RunContext, ModuleResult
from core.registry import discover_module_classes
from core.orchestrator import run_all


class _SampleModule(ModuleInterface):
    key = "sample"
    display_name = "Sample"

    def run(self, ctx):
        return ModuleResult(module_key=self.key, rows_written=1, watermark="2026-09-20", note="ok")


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