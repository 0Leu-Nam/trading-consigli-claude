"""Test del caricamento config.yaml e .env senza chiavi (vincolo 4 del progetto)."""

import pytest

from core import config as cfg


def test_default_config_loads_without_keys():
    conf = cfg.load_config()
    assert "modules" in conf
    assert "database" in conf
    assert conf["database"]["path"] == "data/app.db"
    # Fase 5 attivata: ai 3 moduli della Fase 4 si aggiunge institutional_holdings
    expected_enabled = {
        "insider_trading",
        "price_screener",
        "news_sentiment",
        "institutional_holdings",
    }
    assert all(
        isinstance(item, dict)
        and item.get("enabled") == (key in expected_enabled)
        for key, item in conf["modules"].items()
    )


def test_enabled_modules_returns_all_enabled_modules():
    conf = cfg.load_config()
    assert cfg.enabled_modules(conf) == [
        "insider_trading",
        "price_screener",
        "news_sentiment",
        "institutional_holdings",
    ]


def test_institutional_whitelist_ciks_are_ten_digit_and_unique():
    """I CIK del whitelist sono confrontati con stringhe zfill(10) lato EDGAR.

    Un CIK sbagliato o non normalizzato non solleva errori: semplicemente non
    abbina nessun filing e il modulo produce 0 righe in silenzio. Qui si blocca
    il caso dei dati malformati in config.
    """
    filers = cfg.load_config()["modules"]["institutional_holdings"]["filer_cik_filter"]
    assert filers, "whitelist non vuota: i 3 gestori sono il punto di partenza della Fase 5"
    assert len(filers) == len(set(filers)), "CIK duplicati nel whitelist"
    for cik in filers:
        assert len(cik) == 10, f"CIK non normalizzato a 10 cifre: {cik!r}"
        assert cik.isdigit(), f"CIK non numerico: {cik!r}"


def test_enabled_modules_returns_only_enabled_modules():
    conf = {
        "modules": {
            "a": {"enabled": True},
            "b": {"enabled": False},
            "c": {},
        }
    }
    assert cfg.enabled_modules(conf) == ["a"]


def test_load_env_missing_file_does_not_raise():
    cfg.load_env()  # non deve sollevare errori in assenza di .env
    assert True


def test_get_env_returns_default(monkeypatch):
    monkeypatch.delenv("MARKETAUX_API_TOKEN", raising=False)
    assert cfg.get_env("MARKETAUX_API_TOKEN") is None
    assert cfg.get_env("MARKETAUX_API_TOKEN", "x") == "x"