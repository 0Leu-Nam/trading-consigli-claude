"""Test del caricamento config.yaml e .env senza chiavi (vincolo 4 del progetto)."""

import pytest

from core import config as cfg


def test_default_config_loads_without_keys():
    conf = cfg.load_config()
    assert "modules" in conf
    assert "database" in conf
    assert conf["database"]["path"] == "data/app.db"
    # Fase 4 attivata: insider_trading, price_screener e news_sentiment sono abilitati
    expected_enabled = {"insider_trading", "price_screener", "news_sentiment"}
    assert all(
        isinstance(item, dict)
        and item.get("enabled") == (key in expected_enabled)
        for key, item in conf["modules"].items()
    )


def test_enabled_modules_returns_fase4_modules():
    conf = cfg.load_config()
    assert cfg.enabled_modules(conf) == ["insider_trading", "price_screener", "news_sentiment"]


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