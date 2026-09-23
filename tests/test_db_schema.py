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