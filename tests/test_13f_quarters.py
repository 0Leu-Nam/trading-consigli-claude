"""Test del calcolo dei trimestri 13F (lag ~45gg) e della selezione dei filing."""

from datetime import date

from modules.institutional_holdings import edgar_13f
from modules.institutional_holdings.module import select_filings, target_quarters


def _filing(accession: str, filer_cik: str) -> edgar_13f.Filing13F:
    return edgar_13f.Filing13F(
        accession=accession,
        file_date="2026-08-01",
        period_ending="2026-06-30",
        ciks=[filer_cik],
        display_names=["MGR CAPITAL"],
    )


def test_quarter_within_lag_is_not_processed():
    windows = target_quarters(date(2026, 9, 30), lag_days=45, quarters_back=2)
    assert [w.quarter for w in windows] == ["2026Q2", "2026Q1"]


def test_deposit_window_is_quarter_end_plus_lag():
    window = target_quarters(date(2026, 9, 30), 45, 1)[0]
    assert window.quarter == "2026Q2"
    assert window.deposit_start == date(2026, 7, 1)   # 30 giugno + 1
    assert window.deposit_end == date(2026, 8, 14)    # 30 giugno + 45gg


def test_target_quarters_walks_back_across_year_boundary():
    windows = target_quarters(date(2026, 2, 15), lag_days=45, quarters_back=2)
    assert [w.quarter for w in windows] == ["2025Q4", "2025Q3"]


def test_skipped_quarter_is_recovered_once_lag_expires():
    # Un run saltato non perde dati: il trimestre entra appena il lag scade.
    assert target_quarters(date(2026, 5, 16), 45, 1)[0].quarter == "2026Q1"
    assert target_quarters(date(2026, 5, 15), 45, 1)[0].quarter == "2025Q4"


def test_no_processable_quarter_returns_empty():
    assert target_quarters(date(2026, 9, 30), lag_days=45, quarters_back=0) == []


def test_selection_without_whitelist_takes_most_recent_in_order():
    filings = [_filing("ACC-1", "0000000001"), _filing("ACC-2", "0000000002"), _filing("ACC-3", "0000000003")]
    selected, in_wl = select_filings(filings, max_filings=2, whitelist=set())
    assert [f.accession for f in selected] == ["ACC-1", "ACC-2"]
    assert in_wl == 0


def test_whitelist_filers_are_processed_first():
    filings = [_filing("ACC-1", "0000000001"), _filing("ACC-2", "0000000002"), _filing("ACC-3", "0000000003")]
    selected, in_wl = select_filings(filings, max_filings=2, whitelist={"0000000003"})
    assert [f.accession for f in selected] == ["ACC-3", "ACC-1"]
    assert in_wl == 1


def test_selection_respects_budget_with_many_whitelisted_filers():
    filings = [_filing(f"ACC-{i}", "0000000003") for i in range(1, 6)]
    selected, in_wl = select_filings(filings, max_filings=3, whitelist={"0000000003"})
    assert len(selected) == 3 and in_wl == 3