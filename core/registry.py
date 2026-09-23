"""Discovery dei moduli a plugin.

Convenzione: ogni plugin vive in ``modules/<chiave>/module.py`` ed espone una
classe ``Module(ModuleInterface)`` con ``key`` univoca. Il registry scopre i
moduli in automatico, quindi aggiungere/rimuovere un plugin non richiede di
toccare il core.
"""

import importlib
import inspect
import logging
from pathlib import Path

from core.module_interface import ModuleInterface

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = PROJECT_ROOT / "modules"

logger = logging.getLogger(__name__)


def discover_module_classes() -> dict[str, type[ModuleInterface]]:
    """Scannerizza modules/ e torna {chiave: classe Module} per ogni plugin."""
    found: dict[str, type[ModuleInterface]] = {}
    if not MODULES_DIR.is_dir():
        return found
    for entry in sorted(MODULES_DIR.iterdir()):
        module_py = entry / "module.py"
        if not entry.is_dir() or not module_py.exists():
            continue
        try:
            pkg = importlib.import_module(f"modules.{entry.name}.module")
            cls = getattr(pkg, "Module", None)
            if (
                inspect.isclass(cls)
                and issubclass(cls, ModuleInterface)
                and cls is not ModuleInterface
            ):
                found[cls.key] = cls
        except Exception as exc:  # un plugin rotto non deve bloccare gli altri
            logger.warning("Plugin %s non caricato: %s", entry.name, exc)
    return found


def create_module(key: str) -> ModuleInterface:
    """Istanzia il modulo con la chiave data (solleva KeyError se non esiste)."""
    classes = discover_module_classes()
    if key not in classes:
        raise KeyError(f"Nessun modulo registrato con key '{key}'")
    return classes[key]()