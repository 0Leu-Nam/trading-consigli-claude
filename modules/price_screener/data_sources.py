"""Sorgenti dati OHLCV per il modulo price_screener.

Sorgente primaria: ``yfinance`` (gratis, nessuna chiave), da host locali/CI in
batch. Fallback opzionale: Stooq CSV (richiede ``STOOQ_API_KEY`` nel .env),
usato solo per i ticker che yfinance non restituisce.

Tutti i dati sono normalizzati in :class:`Bar` (date ISO ``YYYY-MM-DD``), in
modo che il modulo sia indipendente dal layout specifico del provider.
"""

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, timedelta

logger = logging.getLogger(__name__)


class PriceSourceError(Exception):
    """Errore irrecuperabile della sorgente dati (rete, auth, risposta vuota)."""


@dataclass(frozen=True)
class Bar:
    date: str          # ISO YYYY-MM-DD
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None


def _to_iso_date(value) -> str:
    """Rende una data Pandas/native (DatetimeIndex o datetime/date) una stringa ISO."""
    import pandas as pd

    dt = pd.Timestamp(value).to_pydatetime()
    return dt.date().isoformat()


def _extract_ticker(df, ticker: str):
    """Estrae le colonne OHLCV di un singolo ticker da un DataFrame yfinance.

    Gestisce sia il batch multi-ticker (colonne MultiIndex ``(Price, Ticker)``)
    sia la modalità single-ticker delle versioni recenti (colonne flat). Se il
    ticker è assente (simbolo scartato da Yahoo) restituisce un DataFrame vuoto.
    """
    import pandas as pd

    if isinstance(df.columns, pd.MultiIndex) and "Ticker" in df.columns.names:
        if ticker in df.columns.get_level_values("Ticker"):
            return df.xs(ticker, axis=1, level="Ticker")
        return df.iloc[0:0]
    return df[["Open", "High", "Low", "Close", "Volume"]]


def _yahoo_alias(ticker: str) -> str:
    """Yahoo usa il trattino per le azioni di classe: BRK.B → BRK-B."""
    return ticker.replace(".", "-")


def _df_to_bars(df) -> list[Bar]:
    """Converte un DataFrame con colonne Open/High/Low/Close/Volume in list[Bar]."""
    import pandas as pd

    bars: list[Bar] = []
    for ts, row in df.iterrows():
        close = row.get("Close")
        if close is None or pd.isna(close):
            continue
        open_ = row.get("Open")
        volume = row.get("Volume")
        bars.append(
            Bar(
                date=_to_iso_date(ts),
                open=None if open_ is None or pd.isna(open_) else float(open_),
                high=None if pd.isna(row.get("High")) else float(row["High"]),
                low=None if pd.isna(row.get("Low")) else float(row["Low"]),
                close=float(close),
                volume=None if volume is None or pd.isna(volume) else int(round(float(volume))),
            )
        )
    return bars


def fetch_yf_history(tickers: list[str], days: int = 30, retries: int = 2) -> dict[str, list[Bar]]:
    """Scarica via yfinance lo storico giornaliero dei ticker. Torna {ticker: [Bar]}.

    Solleva :class:`PriceSourceError` se la sorgente fallisce del tutto; i
    ticker per cui non ci sono dati (delistati, simboli sconosciuti) semplicemente
    mancano dal dict.
    """
    import yfinance as yf

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            raw = yf.download(
                [_yahoo_alias(t) for t in tickers],
                period=f"{max(days, 7)}d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=True,
            )
        except Exception as exc:  # rete, rate limit (YFRateLimitError), ecc.
            last_exc = exc
            logger.warning("yfinance tentativo %d/%d fallito: %s", attempt, retries, exc)
            continue
        if raw is None or len(raw) == 0:
            last_exc = PriceSourceError("yfinance ha restituito un dataset vuoto")
            continue
        # Yahoo chiede il trattino per le azioni di classe (BRK.B → BRK-B):
        # scarichiamo con l'alias ma restituiamo tutto col nome originale.
        return {ticker: _df_to_bars(_extract_ticker(raw, _yahoo_alias(ticker))) for ticker in tickers}
    raise PriceSourceError(f"yfinance non raggiungibile dopo {retries} tentativi: {last_exc}")


STOOQ_URL = "https://stooq.com/q/d/l/"
STOOQ_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) trading-consigli/1.0"


def parse_stooq_csv(text: str, symbol: str) -> list[Bar]:
    """Parsa la CSV storica Stooq (Date,Open,High,Low,Close,Volume) in list[Bar] cronologiche."""
    reader = csv.DictReader(io.StringIO(text))
    bars: list[Bar] = []
    for row in reader:
        date_str = row.get("Date", "").strip()
        close_str = (row.get("Close") or "").strip() or None
        if not date_str or close_str is None:
            continue
        try:
            close = float(close_str)
        except ValueError:
            continue

        def _num(key: str) -> float | None:
            val = (row.get(key) or "").strip()
            try:
                return float(val) if val else None
            except ValueError:
                return None

        vol_raw = (row.get("Volume") or "").strip()
        volume = int(float(vol_raw)) if vol_raw else None
        bars.append(
            Bar(
                date=date_str,
                open=_num("Open"),
                high=_num("High"),
                low=_num("Low"),
                close=close,
                volume=volume,
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars


def fetch_stooq_history(ticker: str, api_key: str, days: int = 30) -> list[Bar]:
    """Fallback Stooq per un singolo ticker (richiede STOOQ_API_KEY)."""
    import requests

    end = date.today()
    start = end - timedelta(days=max(days, 30) + 5)
    params = {
        "s": ticker.lower().replace("-", ".") + ".us",
        "i": "d",
        "d1": start.strftime("%Y%m%d"),
        "d2": end.strftime("%Y%m%d"),
        "apikey": api_key,
    }
    resp = requests.get(STOOQ_URL, params=params, headers={"User-Agent": STOOQ_UA}, timeout=30)
    if resp.status_code != 200 or not resp.text.strip():
        raise PriceSourceError(f"Stooq {ticker}: HTTP {resp.status_code}, risposta vuota")
    if "Exceeded the daily hits limit" in resp.text:
        raise PriceSourceError(f"Stooq {ticker}: superato il limite giornaliero")
    bars = parse_stooq_csv(resp.text, ticker)
    if not bars:
        raise PriceSourceError(f"Stooq {ticker}: nessuna barra storica")
    return bars


def fetch_history(
    tickers: list[str],
    days: int = 30,
    *,
    stooq_key: str | None = None,
) -> tuple[dict[str, list[Bar]], list[str]]:
    """API principale del modulo: torna (bars_per_ticker, ticker_senza_dati).

    Prova prima yfinance in batch; i ticker vuoti vengono ritentati su Stooq
    solo se è disponibile una chiave. Gli errori irrecuperabili della sorgente
    primaria risalgono come :class:`PriceSourceError`.
    """
    bars_map = fetch_yf_history(tickers, days=days)
    missing = [t for t, bars in bars_map.items() if not bars]
    if missing and stooq_key:
        for ticker in missing:
            try:
                bars_map[ticker] = fetch_stooq_history(ticker, stooq_key, days=days)
            except PriceSourceError as exc:
                logger.warning("Stooq %s non disponibile: %s", ticker, exc)
    return bars_map, missing