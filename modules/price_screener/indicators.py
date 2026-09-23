"""Indicatori di prezzo/volume, puramente funzionali (facili da testare).

Non fanno rete né accedono al DB: ricevono i prezzi e restituiscono i valori
numerici. Il modulo usa ``build_snapshot_rows`` per allineare una barra ad
ogni giorno e ritagliare la finestra da salvare in ``price_snapshots``.
"""

from modules.price_screener.data_sources import Bar

VOLUME_WINDOW = 20  # media mobile di volume "fino ad oggi", giorni di borsa


def pct_return(closes: list[float | None], i: int, n: int) -> float | None:
    """Rendimento netto tra closes[i-n] e closes[i] (n giorni di borsa)."""
    if i - n < 0:
        return None
    prev, cur = closes[i - n], closes[i]
    if prev is None or cur is None or prev == 0:
        return None
    return (cur / prev) - 1.0


def volume_ratio(volumes: list[int | None], i: int, window: int = VOLUME_WINDOW) -> float | None:
    """Volume di i diviso la media dei `window` volumi precedenti (escluso i)."""
    if i - window < 0:
        return None
    hist = volumes[i - window : i]
    if not hist or any(v is None or v == 0 for v in hist):
        return None
    cur = volumes[i]
    if cur is None:
        return None
    return cur / (sum(hist) / len(hist))


def _bar_row(b: Bar, abs_1d: float | None, abs_5d: float | None, vol_ratio: float | None) -> dict:
    return {
        "date": b.date,
        "open": b.open,
        "high": b.high,
        "low": b.low,
        "close": b.close,
        "volume": b.volume,
        "abs_return_1d": abs_1d,
        "abs_return_5d": abs_5d,
        "vol_vs_avg_20": vol_ratio,
    }


def build_snapshot_rows(bars: list[Bar], history_days: int = 25) -> list[dict]:
    """Allinea gli indicatori ad ogni barra e ritaglia le ultime `history_days`.

    Gli indicatori si calcolano sull'intero storico ricevuto (serve lookback
    per le medie), poi si conservano solo le barre più recenti per contenere
    il disco: un run giornaliero su ~500 ticker mantiene ~500×history_days righe.
    """
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    rows = [
        _bar_row(b, pct_return(closes, i, 1), pct_return(closes, i, 5), volume_ratio(volumes, i))
        for i, b in enumerate(bars)
    ]
    if history_days and history_days > 0:
        rows = rows[-history_days:]
    return rows