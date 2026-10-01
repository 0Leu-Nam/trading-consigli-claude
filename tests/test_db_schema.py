"""Test dello schema SQLite: creazione pulita, idempotenza, vincoli."""

import sqlite3

import pytest

from core import db


@pytest.fixture()
def db_file(tmp_path):
    path = tmp_path / "test.db"
    yield path


def test_init_schema_creates_all_tables(db_file):
    db.init_schema(db_file)
    assert db_file.exists()
    assert db.table_names(db_file) == db.EXPECTED_TABLES


def test_init_schema_is_idempotent(db_file):
    db.init_schema(db_file)
    db.init_schema(db_file)
    assert db.table_names(db_file) == db.EXPECTED_TABLES


def test_wal_and_foreign_keys_enabled(db_file):
    db.init_schema(db_file)
    with db.connect(db_file) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_companies_cik_partial_unique_index(db_file):
    db.init_schema(db_file)
    with db.connect(db_file) as conn:
        conn.execute(
            "INSERT INTO companies (ticker, name, cik, updated_at) VALUES ('AAA', 'A', '1', '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (ticker, name, cik, updated_at) VALUES ('AAB', 'B', NULL, '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (ticker, name, cik, updated_at) VALUES ('AAC', 'C', NULL, '2024-01-01')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO companies (ticker, name, cik, updated_at) VALUES ('AAD', 'D', '1', '2024-01-01')"
            )


def test_insider_accession_unique_prevents_duplicates(db_file):
    db.init_schema(db_file)
    with db.connect(db_file) as conn:
        conn.execute(
            "INSERT INTO companies (ticker, updated_at) VALUES ('AAA', '2024-01-01')"
        )
        company_id = conn.execute("SELECT id FROM companies LIMIT 1").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO insider_transactions
                (company_id, accession, filing_date)
            VALUES (?, '0000000000-24-000001', '2024-01-02')
            """,
            (company_id,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO insider_transactions
                    (company_id, accession, filing_date)
                VALUES (?, '0000000000-24-000001', '2024-01-03')
                """,
                (company_id,),
            )


def test_foreign_key_cascade(db_file):
    db.init_schema(db_file)
    with db.connect(db_file) as conn:
        conn.execute(
            "INSERT INTO companies (ticker, updated_at) VALUES ('AAA', '2024-01-01')"
        )
        company_id = conn.execute("SELECT id FROM companies LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO watchlist (company_id) VALUES (?)",
            (company_id,),
        )
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


def test_schema_v2_holdings_columns_and_cusip_lookup(db_file):
    db.init_schema(db_file)
    with db.connect(db_file) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(institutional_holdings)")}
        assert {"cusip", "filer_cik", "issuer_name"} <= cols
        assert "cik" not in cols  # v2: il cik del gestore vive in filer_cik
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM cusip_lookup").fetchone()[0] == 0


V1_HOLDINGS_DDL = """
CREATE TABLE institutional_holdings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    filing_quarter TEXT NOT NULL,
    filing_date    TEXT,
    filer_name     TEXT,
    cik            TEXT,
    shares         INTEGER,
    value_usd      INTEGER,
    shares_delta   INTEGER,
    UNIQUE (filing_quarter, filer_name, company_id)
);
"""


def test_migrate_v1_to_v2_preserves_rows_and_new_constraints(db_file):
    db.init_schema(db_file)
    with db.connect(db_file) as conn:
        conn.execute("DROP TABLE institutional_holdings")  # simuliamo uno schema v1
        conn.execute(V1_HOLDINGS_DDL)
        conn.execute(
            "INSERT INTO companies (ticker, name, updated_at)"
            " VALUES ('ABBV', 'ABBVIE INC', '2024-01-01')"
        )
        company_id = conn.execute("SELECT id FROM companies").fetchone()["id"]
        conn.execute(
            """INSERT INTO institutional_holdings
                 (company_id, filing_quarter, filing_date, filer_name, cik, shares, value_usd)
               VALUES (?, '2026Q1', '2026-05-12', 'ACME CAPITAL', '0000000001', 2000, 400000)""",
            (company_id,),
        )
        conn.commit()

    db.init_schema(db_file)  # deve migrare

    with db.connect(db_file) as conn:
        row = conn.execute(
            """SELECT company_id, filer_name, filer_cik, cusip, shares, value_usd
               FROM institutional_holdings"""
        ).fetchone()
        assert row["company_id"] == company_id
        assert row["filer_name"] == "ACME CAPITAL"
        assert row["filer_cik"] == "0000000001"  # cik (v1) migrato in filer_cik
        assert row["cusip"] is None
        assert row["shares"] == 2000

        # La nuova UNIQUE (filing_quarter, filer_cik, cusip) è in vigore
        insert = (
            "INSERT INTO institutional_holdings (company_id, filing_quarter, filer_cik, cusip)"
            " VALUES (?, '2026Q2', '0000000001', 'X1')"
        )
        conn.execute(insert, (company_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (company_id,))

    assert db.table_names(db_file) == db.EXPECTED_TABLES


def test_migrate_is_idempotent_on_v2(db_file):
    db.init_schema(db_file)
    db.init_schema(db_file)
    with db.connect(db_file) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(institutional_holdings)")}
        assert "cusip" in cols
    assert db.table_names(db_file) == db.EXPECTED_TABLES