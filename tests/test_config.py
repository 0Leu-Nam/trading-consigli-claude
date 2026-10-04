"""Test del caricamento config.yaml e .env senza chiavi (vincolo 4 del progetto)."""

import pytest

from core import config as cfg


def test_default_config_loads_without_keys():
    conf = cfg.load_config()
    assert "modules" in conf
    assert "database" in conf
    assert conf["database"]["path"] == "data/app.db"
    # Fase 5 e Fase 6 attivate in produzione. La convenzione del progetto e'
    # che un modulo nuovo non parte senza verifica: scoring e' stato tenuto
    # `enabled: false` fino all'osservazione locale conclusa (220/220 test e
    # shortlist a due sezioni riletta sui dati reali).
    expected_enabled = {
        "insider_trading",
        "price_screener",
        "news_sentiment",
        "institutional_holdings",
        "scoring",
    }
    assert all(
        isinstance(item, dict)
        and item.get("enabled") == (key in expected_enabled)
        for key, item in conf["modules"].items()
    ), (
        "un modulo atteso disabilitato risulta abilitato, o viceversa: "
        "l'elenco degli attesi va tenuto allineato a config.yaml"
    )
    assert "scoring" in conf["modules"], "il modulo scoring deve essere in config"
    assert conf["modules"]["scoring"]["enabled"] is True, (
        "scoring e' attivato in produzione dal 2026-10-04: se questo test torna "
        "rosso, il flip e' stato annullato senza una decisione esplicita"
    )


def test_enabled_modules_returns_all_enabled_modules():
    conf = cfg.load_config()
    assert cfg.enabled_modules(conf) == [
        "insider_trading",
        "price_screener",
        "news_sentiment",
        "institutional_holdings",
        "scoring",
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

    # Le due sezioni di shortlist hanno soglie e tetti separati. `single_source_min`
    # non puo' mancare: senza, il modulo la tratta come "sezione disabilitata" e i
    # sei ticker a +45 della finestra reale tornano fuori dalla shortlist senza che
    # nessuno lo legga da config.
    assert "single_source_min" in scoring, "manca la soglia della sezione a fonte singola"
    assert scoring["single_source_min"] > scoring["min_signals"], (
        "la soglia del fonte singola e' uno score, non un conteggio di moduli"
    )
    assert scoring["single_source_limit"] > 0, "serve un tetto alla sezione fonte singola"
    assert scoring["shortlist_size"] > 0


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
        # La banda trascurabile (`lt`) sta FUORI dal controllo di discesa: e'
        # un tetto, non un floor, e il suo 0.01 non ha niente a che fare con
        # l'ordine delle soglie 0.50 / 0.20 / 0.05 / 0.0 sotto di lei. Misurarla
        # insieme produrrebbe un falso "le soglie non scendono".
        tetti = [b for b in bande if "lt" in b]
        assert len(tetti) == 1, f"{lato}: serve una sola banda trascurabile, non {len(tetti)}"
        assert tetti[0] == bande[0], f"{lato}: la banda 'lt' deve essere la prima, altrimenti non viene mai valutata"
        soglie = [b["gte"] for b in bande if "gte" in b]
        assert soglie == sorted(soglie, reverse=True), f"{lato}: le soglie devono scendere"
        assert soglie[-1] <= 0.0, f"{lato}: l'ultima voce e' il piano, deve coprire il minimo"
        assert all(b.get("mult", 0) > 0 for b in bande), f"{lato}: moltiplicatore mancante"

    # La banda trascurabile deve essere uguale da entrambi i lati: comprare lo
    # 0.5% della propria posizione non dimostra convinzione nemmeno quando
    # l'importo e' grosso, quindi asimmetria qui significherebbe che la
    # simmetria scelta non e' mai stata applicata davvero.
    assert scale["frac_buy"][0] == scale["frac_sell"][0], (
        "la soglia trascurabile deve essere simmetrica su acquisti e vendite"
    )
    assert 0 < scale["frac_buy"][0]["lt"] <= 0.02, (
        "la soglia trascurabile deve restare sotto il 2%: ABEO (1.501%) e' una "
        "vendita reale e a 2% verrebbe sommersa insieme al rumore (AFL 0.018%)"
    )

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
    # ...ma non deve tagliare i segnali che ci sono davvero: con 45 sparivano
    # sei acquisti reali, per clippare contributi che valevano 55.2 (XENE, ADRX).
    assert scale["max_abs"] >= 56, "45 tagliava segnali reali a 55.2: il tetto era troppo basso"


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