"""Atomic file I/O helpers + SQLite connection factory.

Ported verbatim from cu-permits (platform-agnostic); only DB_PATH changes to the
San Carlos database name.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "outputs" / "sca_permits.db"
MIGRATIONS_DIR = ROOT / "migrations"


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    tmp.replace(path)


def atomic_write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})
    tmp.replace(path)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection in WAL mode. Auto-creates parent dir."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def apply_migrations(conn: sqlite3.Connection,
                     migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every .sql file in migrations_dir in lexical order, skipping
    files already applied (tracked in _schema_migrations).

    Returns the list of files applied this call (empty if all up to date).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    already = {row[0] for row in conn.execute(
        "SELECT filename FROM _schema_migrations")}
    applied: list[str] = []
    for f in sorted(migrations_dir.glob("*.sql")):
        if f.name in already:
            continue
        sql = f.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO _schema_migrations (filename, applied_at) "
            "VALUES (?, datetime('now'))", (f.name,))
        applied.append(f.name)
    conn.commit()
    return applied
