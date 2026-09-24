"""Sentiment a costo zero: lessico di parole (EN) + funzioni pure.

Il modulo usa il risultato di Marketaux quando disponibile; qui sta il
fallback calculation per le fonti che non offrono un sentiment (es. RSS).
Score normalizzato in [-1, 1] e label per soglia configurabile ``sentiment_gte``.
Nessuna rete, nessun DB: solo testo → numeri.
"""

import re

POSITIVE_WORDS = {
    "beat", "beats", "upgrade", "upgrades", "raised", "raises", "gain", "gains",
    "rally", "rallies", "surge", "surges", "surging", "jump", "jumps", "jumped",
    "rise", "rises", "rose", "soar", "soars", "soared", "record", "profit",
    "profits", "growth", "growing", "dividend", "boost", "boosts", "strong",
    "bullish", "win", "wins", "won", "buy", "buying", "optimistic", "outperform",
    "expansion", "expanding", "recovery", "rebound", "milestone", "breakthrough",
    "partnership", "acquisition", "increases", "increase", "higher", "tops",
    "outstanding", "best", "jumps", "climbs", "advances", "advance", "positive",
}

NEGATIVE_WORDS = {
    "miss", "misses", "downgrade", "downgrades", "cut", "cuts", "slump", "slumps",
    "plunge", "plunges", "plunged", "falls", "fall", "fell", "frozen", "drop",
    "drops", "dropped", "decline", "declines", "down", "loss", "losses", "lawsuit",
    "probe", "probes", "risk", "risks", "warning", "warns", "warned", "weak",
    "weaker", "weakness", "sell", "selling", "bearish", "hit", "hits", "slides",
    "slide", "sinks", "sank", "tumble", "tumbles", "tumbled", "crash", "crashes",
    "fail", "fails", "failed", "failing", "layoffs", "layoff", "concern",
    "concerns", "penalty", "worst", "suspension", "investigation", "misses",
    "lower", "downgrades", "negative", "uncertainty", "disappointing", "slashed",
    "lawsuit", "probe", "delay", "delays", "halt", "halts", "fires", "ousts",
}

_TOKEN_RE = re.compile(r"[a-z']+")


def tokenize(text: str | None) -> set[str]:
    """Estrae le parole uniche in minuscolo da un testo (punteggiatura ignorata)."""
    if not text:
        return set()
    return set(_TOKEN_RE.findall(text.lower()))


def sentiment_score(text: str | None) -> float:
    """Score in [-1, 1]: (positivi - negativi) / (positivi + negativi).

    Testo senza parole del lessico → 0.0 (neutro). Funziona anche su titoli
    corti: bastano una parola positiva e una negativa perché lo score sia -1/1.
    """
    words = tokenize(text)
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    if pos == 0 and neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 4)


def sentiment_label(score: float | None, gte: float = 0.35) -> str | None:
    """Label per soglia: positive/negative/neutral; None se score è None."""
    if score is None:
        return None
    if score >= gte:
        return "positive"
    if score <= -gte:
        return "negative"
    return "neutral"