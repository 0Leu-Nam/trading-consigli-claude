"""Contratto comune a tutti i moduli (plugin).

Ogni modulo espone, in ``modules/<nome>/module.py``, una classe ``Module``
sottoclasse di :class:`ModuleInterface`. L'orchestratore NON conosce i dettagli
interni: istanzia il modulo, gli passa un :class:`RunContext` e legge il
:class:`ModuleResult` restituito.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

import sqlite3


@dataclass
class ModuleResult:
    """Esito standardizzato di un run. Tutti i campi sono opzionali.

    ``watermark`` viene scritto dall'orchestratore su ``module_state``
    (il modulo quindi NON tocca mai module_state direttamente).
    """

    module_key: str
    status: str = "ok"          # 'ok' | 'error' | 'skipped'
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)
    watermark: Optional[str] = None
    note: str = ""


class RunContext:
    """Ciò che un modulo riceve in input: connessione DB, config del modulo,
    config globale e lettura di variabili d'ambiente (mai nomi di chiavi hardcoded)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        module_config: dict,
        global_config: dict,
        env_getter: Callable[[str, Optional[str]], Optional[str]],
    ) -> None:
        self.conn = conn
        self.module_config = module_config or {}
        self.global_config = global_config or {}
        self._env_getter = env_getter

    def get(self, key: str, default=None):
        """Legge un parametro dalla sezione del modulo in config.yaml."""
        return self.module_config.get(key, default)

    def env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Legge una chiave da ambiente/.env (mai hardcoded nel codice)."""
        return self._env_getter(key, default)

    @property
    def db(self) -> sqlite3.Connection:
        return self.conn


class ModuleInterface(ABC):
    """Classe base astratta: l'unico contratto tra orchestatore e moduli."""

    key: str = ""
    display_name: str = ""

    @abstractmethod
    def run(self, ctx: RunContext) -> ModuleResult:
        """Esegue il ciclo di raccolta/analisi del modulo."""
        raise NotImplementedError