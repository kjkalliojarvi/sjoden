import duckdb
from pathlib import Path

from sjoden.archive_db import db_ops, create

def test_db_ops_creates_directory_and_connects(tmp_path: Path):
    db_file = tmp_path / "new_dir" / "test.duckdb"

    # Assert dir does not exist
    assert not db_file.parent.exists()

    with db_ops(str(db_file)) as conn:
        assert isinstance(conn, duckdb.DuckDBPyConnection)
        # Test connection works
        conn.execute("CREATE TABLE t (id INT)")
        conn.execute("INSERT INTO t VALUES (1)")

    # Assert dir was created and file exists
    assert db_file.parent.exists()
    assert db_file.exists()

    # Verify commit happened by reading the data back
    with duckdb.connect(str(db_file)) as conn:
        res = conn.execute("SELECT id FROM t").fetchall()
        assert res == [(1,)]

def test_create_initializes_schema():
    conn = duckdb.connect(":memory:")
    create(conn)

    # Check if the 'atg' schema was created
    schemas = [r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata").fetchall()]
    assert "atg" in schemas

    # Check if tables were created
    tables = [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'atg'").fetchall()]
    assert "race" in tables
    assert "start" in tables
    assert "horse" in tables
    assert "person" in tables
    assert "bet_distribution" in tables
    assert "pool" in tables

    # Check idempotency - should not fail when called again
    create(conn)
