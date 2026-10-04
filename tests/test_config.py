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
    # l'istituzionale e' trimestrale: il suo peso viene confrontato con segnali
    # settimanali e per questo non porta un peso proprio
    assert set(scoring["windows"]["institutional"]) == {"quarters"}, (
        "la finestra istituzionale deve dichiarare solo 'quarters'"
    )
    # NON deve esserci un gate di freschezza: un 13F vecchio resta un'informazione
    # vera e viene dichiarato con la sua eta' nella description. Se la chiave
    # tornasse in config, il modulo la ignorerebbe in silenzio e il peso
    # dell'istituzionale cambierebbe comportamento senza che nessuno lo legga.
    assert "max_age_days" not in scoring["windows"]["institutional"], (
        "gate di freschezza riintrodotto: l'eta' va in description, non in condizione"
    )
    assert scoring["warn_institutional_age_days"] > 0, "serve una soglia per l'avviso in note"


def test_scoring_insider_scale_is_complete_and_ordered():
    """La scala del contributo insider.

    Le bande devono scendere e l'ultima deve essere il piano: senza una voce che
    copre il valore minimo, una frazione del 2% resterebbe senza moltiplicatore
    (o con l'1.0 implicitto, che e' un peso fisso travestito). Uguale per
    acquisto e vendita, perche' la quota di posizione si misura nello stesso
    modo nei due casi.
    """
    scale = cfg.load_config()["modules"]["scoring"]["insider_scale"]
    assert set(scale) == {"frac_buy", "frac_sell", "by_insider_count", "usd_fallback", "max_abs"}

    for lato in ("frac_buy", "frac_sell"):
        bande = scale[lato]
        assert len(bande) >= 2, f"{lato}: serve almeno una soglia e un piano"
        soglie = [b["gte"] for b in bande]
        assert soglie == sorted(soglie, reverse=True), f"{lato}: le soglie devono scendere"
        assert soglie[-1] <= 0.0, f"{lato}: l'ultima voce e' il piano, deve coprire il minimo"
        assert all(b.get("mult", 0) > 0 for b in bande), f"{lato}: moltiplicatore mancante"

    conteggio = [b["gte"] for b in scale["by_insider_count"]]
    assert conteggio[0] >= 4 and conteggio[-1] == 1, "la banda sul numero di persone deve partire da 4 e finire a 1"

    for lato in ("buy", "sell"):
        fallback = scale["usd_fallback"][lato]
        assert [b["gte_usd"] for b in fallback] == sorted(
            (b["gte_usd"] for b in fallback), reverse=True
        ), f"usd_fallback.{lato}: le soglie devono scendere"
        assert fallback[-1]["gte_usd"] == 0, f"usd_fallback.{lato}: manca il piano a zero"

    # il tetto assoluto: senza, 30 x 1.6 x 1.35 = 64.8 da un solo modulo e
    # i 4 moduli insieme arriverebbero ben oltre la scala dei 100 punti
    assert scale["max_abs"] > 0
    peggiore = (
        max(b["mult"] for b in scale["frac_buy"])
        * max(b["mult"] for b in scale["by_insider_count"])
    )
    assert scale["max_abs"] < 30 * peggiore, "il tetto deve tagliare davvero, non essere decorativo"


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