"""Test delle funzioni pure di indicatori prezzo/volume (nessuna rete, nessun DB)."""

import pytest

from modules.price_screener import indicators
from modules.price_screener.data_sources import Bar


def _bars(closes, volumes=None, start="2026-09-01"):
    vols = volumes or [None] * len(closes)
    return [
        Bar(date=f"{start[:8]}{i + 1:02d}", open=c, high=c, low=c, close=c, volume=v)
        for i, (c, v) in enumerate(zip(closes, vols))
    ]


# ── pct_return ───────────────────────────────────────────────────────────────

def test_pct_return_basic():
    assert indicators.pct_return([100.0, 110.0], 1, 1) == pytest.approx(0.1)
    assert indicators.pct_return([100.0, 90.0], 1, 1) == pytest.approx(-0.1)


def test_pct_return_too_short_window():
    assert indicators.pct_return([100.0, 110.0], 1, 5) is None
    assert indicators.pct_return([100.0], 0, 1) is None


def test_pct_return_missing_or_zero_prev():
    assert indicators.pct_return([None, 110.0], 1, 1) is None
    assert indicators.pct_return([100.0, None], 1, 1) is None
    assert indicators.pct_return([0.0, 110.0], 1, 1) is None


def test_pct_return_5d():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 125.0]
    assert indicators.pct_return(closes, 5, 5) == pytest.approx(0.25)


# ── volume_ratio ─────────────────────────────────────────────────────────────

def test_volume_ratio_basic():
    vols = [1_000_000] * 20 + [3_000_000]
    assert indicators.volume_ratio(vols, 20, window=20) == pytest.approx(3.0)


def test_volume_ratio_insufficient_window():
    vols = [1_000_000] * 10
    assert indicators.volume_ratio(vols, 9, window=20) is None


def test_volume_ratio_missing_volume():
    vols = [None] * 20 + [3_000_000]
    assert indicators.volume_ratio(vols, 20, window=20) is None
    vols = [1_000_000] * 20 + [None]
    assert indicators.volume_ratio(vols, 20, window=20) is None


# ── build_snapshot_rows ──────────────────────────────────────────────────────

def test_build_rows_keeps_last_n_and_aligns_indicators():
    closes = list(range(100, 140))  # 40 barre, +1% a barra
    vols = [1_000_000] * 40
    rows = indicators.build_snapshot_rows(_bars(closes, vols), history_days=5)
    assert len(rows) == 5
    assert rows[-1]["close"] == 139.0
    assert rows[-1]["abs_return_1d"] == pytest.approx(1.0 / 138.0)
    assert rows[-1]["abs_return_5d"] == pytest.approx(5.0 / 134.0)
    assert rows[-1]["vol_vs_avg_20"] == pytest.approx(1.0)


def test_build_rows_early_bars_have_none_indicators():
    closes = [100.0, 110.0]
    vols = [1_000_000, 2_000_000]
    rows = indicators.build_snapshot_rows(_bars(closes, vols), history_days=10)
    assert len(rows) == 2
    assert rows[0]["abs_return_1d"] is None
    assert rows[0]["vol_vs_avg_20"] is None
    assert rows[1]["abs_return_1d"] == pytest.approx(0.1)


def test_build_rows_no_trim_when_history_days_gt_length():
    closes = [100.0, 110.0, 120.0]
    rows = indicators.build_snapshot_rows(_bars(closes), history_days=25)
    assert len(rows) == 3