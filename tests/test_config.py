"""Test del caricamento config.yaml e .env senza chiavi (vincolo 4 del progetto)."""

import pytest

from core import config as cfg


def test_default_config_loads_without_keys():
    conf = cfg.load_config()
    assert "modules" in conf
    assert "database" in conf
    assert conf["database"]["path"] == "data/app.db"
    # Fase 5 attivata in produzione. Fase 6 (scoring) esiste ma resta
    # enabled: false finche' non e' stata osservata in locale: la convenzione
    # del progetto e' che un modulo nuovo non parte senza verifica.
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
    assert "scoring" in conf["modules"], "il modulo scoring deve essere in config"
    assert conf["modules"]["scoring"]["enabled"] is False, (
        "scoring deve restare disabilitato finche' non e' stato osservato"
    )


def test_enabled_modules_returns_all_enabled_modules():
    conf = cfg.load_config()
    assert cfg.enabled_modules(conf) == [
        "insider_trading",
        "price_screener",
        "news_sentiment",
        "institutional_holdings",
    ]


def test_scoring_weights_are_configured_and_documented():
    """Ogni regola di scoring ha il suo peso in config e nessun peso e' lasciato
    fuori: un peso mancante significa un segnale che non esiste e non genera
    nessun errore, quindi va bloccato qui."""
    scoring = cfg.load_config()["modules"]["scoring"]
    weights = scoring["weights"]
    expected = {
        "insider_open_market_buy",
        "insider_open_market_sell",
        "price_volume_spike",
        "price_move_up",
        "news_sentiment_positive",
        "news_sentiment_negative",
        "institutional_new_position",
        "institutional_increase",
        "institutional_multiple",
    }
    assert expected <= set(weights), f"pesi mancanti: {sorted(expected - set(weights))}"
    # positivi e negativi devono essere entrambi presenti: senza segno negativo
    # lo scoring premia sempre e la shortlist perde il senso
    assert any(w < 0 for w in weights.values()), "nessun peso negativo: tutto premerebbe"
    assert any(w > 0 for w in weights.values())
    # ogni modulo con dati in produzione deve avere una finestra dichiarata
    for module_key in ("insider", "price", "news", "institutional"):
        assert module_key in scoring["windows"], f"finestra mancante per {module_key}"
    # l'istituzionale e' trimestrale: senza max_age_days il suo peso finirebbe
    # per essere confrontato con segnale settimanali
    assert "max_age_days" in scoring["windows"]["institutional"]


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