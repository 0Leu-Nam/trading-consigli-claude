"""Orchestratore: esegue in sequenza i moduli attivi definiti in config.yaml.

NON conosce i dettagli interni dei moduli: li istanzia dal registry, passa il
``RunContext``, colleziona i ``ModuleResult`` e aggiorna ``run_log`` e
``module_state``. Un errore in un modulo non impedisce l'esecuzione dei
successivi.
"""

import json
import logging
from pathlib import Path

from core import db
from core.config import load_config, load_env, enabled_modules, get_env
from core.module_interface import RunContext, ModuleResult
from core.registry import discover_module_classes

logger = logging.getLogger(__name__)


def run_module(key: str, conn, config: dict, env_getter) -> ModuleResult:
    """Esegue un singolo modulo attivo, gestendo le eccezioni in modo isolato."""
    from core.registry import create_module

    module_config = config.get("modules", {}).get(key, {})
    ctx = RunContext(
        conn=conn,
        module_config=module_config,
        global_config=config,
        env_getter=env_getter,
    )
    try:
        module = create_module(key)
        logger.info("Esecuzione modulo: %s", key)
        result = module.run(ctx)
    except Exception as exc:
        logger.exception("Modulo %s fallito", key)
        result = ModuleResult(module_key=key, status="error", errors=[str(exc)])
    db.upsert_module_state(
        conn,
        result.module_key,
        last_run_at=db.utcnow_iso(),
        last_processed_id=result.watermark,
        state_json=json.dumps({"status": result.status, "note": result.note}),
    )
    return result


def run_all(config: dict | None = None, db_path: Path | str | None = None) -> tuple[list[ModuleResult], int]:
    """Esegue tutti i moduli abilitati. Torna (risultati, run_id del run_log)."""
    load_env()
    conf = config if config is not None else load_config()
    path = Path(db_path if db_path is not None else conf["database"]["path"])
    db.init_schema(path)

    keys = enabled_modules(conf)
    started = db.utcnow_iso()
    conn = db.connect(path)
    try:
        run_id = db.start_run_log(conn, started, keys)
        conn.commit()

        results: list[ModuleResult] = []
        for key in keys:
            results.append(run_module(key, conn, conf, get_env))
            conn.commit()

        errors = [f"{r.module_key}: {err}" for r in results for err in r.errors]
        status = "ok" if not errors else "error"
        db.finish_run_log(conn, run_id, status=status, finished_at=db.utcnow_iso(), errors=errors)
        conn.commit()
        return results, run_id
    finally:
        conn.close()


def available_modules() -> list[str]:
    return sorted(discover_module_classes().keys())