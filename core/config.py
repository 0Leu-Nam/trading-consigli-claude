"""Caricamento centralizzato di configurazione e segreti.

- ``config.yaml``: parametri di run e abilitazione/disabilitazione moduli.
- ``.env`` (gitignored): eventuali chiavi API, caricato con ``python-dotenv``.
Nessun dato sensibile è letto da config.yaml: solo da .env / environment.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def load_config(path: Path | str | None = None) -> dict:
    """Carica e returna config.yaml come dict. Lancia FileNotFoundError se manca."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_env(env_path: Path | str | None = None) -> None:
    """Carica il file .env (se presente) nelle variabili d'ambiente. Mai errore se assente."""
    dot_env = Path(env_path) if env_path else DEFAULT_ENV_PATH
    load_dotenv(dotenv_path=dot_env, override=False)


def get_env(key: str, default: str | None = None) -> str | None:
    """Legge una variabile d'ambiente (dopo eventuale load_env())."""
    return os.getenv(key, default)


def enabled_modules(config: dict | None = None) -> list[str]:
    """Lista delle chiavi dei moduli con ``enabled: true`` in config.yaml."""
    cfg = config if config is not None else load_config()
    return [
        key
        for key, value in cfg.get("modules", {}).items()
        if isinstance(value, dict) and value.get("enabled")
    ]