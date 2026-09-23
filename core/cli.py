"""Interfaccia a riga di comando.

Uso:
    python -m core.cli run-all
    python -m core.cli run <module_key>
    python -m core.cli status
"""

import argparse
import sys

from core import db
from core.config import load_config, load_env, get_env, enabled_modules


def _connect():
    load_env()
    conf = load_config()
    path = conf["database"]["path"]
    db.init_schema(path)
    return conf, db.connect(path)


def cmd_run_all(_args) -> int:
    from core.orchestrator import run_all

    results, run_id = run_all()
    print(f"run_id={run_id}")
    for r in results:
        print(f"  [{r.status}] {r.module_key}: rows={r.rows_written} note={r.note or '-'}")
        for err in r.errors:
            print(f"      ERRORE: {err}")
    return 0 if all(r.status != "error" for r in results) else 1


def cmd_run(args) -> int:
    from core.orchestrator import run_module

    conf, conn = _connect()
    key = args.module_key
    if key not in enabled_modules(conf):
        print(f"Il modulo '{key}' non è abilitato in config.yaml (enabled: false)", file=sys.stderr)
        return 2
    result = run_module(key, conn, conf, get_env)
    conn.commit()
    conn.close()
    print(f"[{result.status}] {result.module_key}: rows={result.rows_written} note={result.note or '-'}")
    for err in result.errors:
        print(f"  ERRORE: {err}", file=sys.stderr)
    return 0 if result.status != "error" else 1


def cmd_status(_args) -> int:
    from core.orchestrator import available_modules

    conf, conn = _connect()
    print(f"DB: {conf['database']['path']} (esiste: {db.db_exists(conf['database']['path'])})")
    print("Moduli disponibili:", ", ".join(available_modules()) or "(nessuno)")
    print("Moduli attivi:", ", ".join(enabled_modules(conf)) or "(nessuno)")
    for table in sorted(db.table_names(conf["database"]["path"])):
        try:
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except Exception:
            count = "?"
        print(f"  {table:<26} righe={count}")
    print("Ultimi run:")
    for row in conn.execute(
        "SELECT id, started_at, finished_at, status, modules_run FROM run_log ORDER BY id DESC LIMIT 5"
    ):
        print(f"  #{row['id']} {row['started_at']} -> {row['status']} modules={row['modules_run']}")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trading-consigli-claude")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run-all", help="Esegue tutti i moduli abilitati in config.yaml")
    p_run = sub.add_parser("run", help="Esegue un singolo modulo abilitato")
    p_run.add_argument("module_key")
    sub.add_parser("status", help="Mostra stato di DB, moduli e run log")
    args = parser.parse_args(argv)

    handlers = {"run-all": cmd_run_all, "run": cmd_run, "status": cmd_status}
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())