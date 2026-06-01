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


def load_run_ledger(path: Path, log=print) -> dict:
    """Read a step's append-only run ledger ({"schema_version": 1, "runs": [...]}),
    tolerating damage by starting fresh.

    Every step does `load_json(ledger, default)` then `runs["runs"].append(...)`.
    But load_json only defaults on a MISSING file, so a corrupt/truncated ledger
    raises, and a valid-JSON-but-wrong-shape one (e.g. a bare list, or no "runs"
    key) crashes the .append -- in BOTH cases aborting the step purely over its
    audit log, AFTER its real DB work committed. For the critical step3 that also
    skips the clustering + sync that follow. A damaged audit log must never do
    that: start a fresh ledger (losing history is strictly better than aborting)
    and warn. Missing file -> fresh, silently (the normal first-run case)."""
    default = {"schema_version": 1, "runs": []}
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"  WARNING: run ledger {path.name} unreadable ({str(exc)[:60]}); "
            f"starting a fresh one")
        return default
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        log(f"  WARNING: run ledger {path.name} has unexpected shape; "
            f"starting a fresh one")
        return default
    return data


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
        # Apply each migration ATOMICALLY: the file's DDL and its bookkeeping row
        # commit together, or neither does. executescript() otherwise runs in
        # autocommit, so a multi-statement file (e.g. 0004's ten ADD COLUMNs) that
        # failed partway would leave some columns added but the file unrecorded --
        # and since ALTER TABLE ADD COLUMN isn't idempotent, the retry would re-run
        # the already-applied ADD COLUMN and crash with "duplicate column",
        # bricking the pipeline. Wrapping in BEGIN/COMMIT (with rollback on error)
        # makes a partial failure a clean no-op the next run can retry. The
        # bookkeeping INSERT lives inside the same transaction; the filename is
        # ours (migrations dir) but we still escape quotes defensively since
        # executescript() can't bind parameters.
        fname = f.name.replace("'", "''")
        script = (
            "BEGIN;\n"
            + sql
            + "\nINSERT INTO _schema_migrations (filename, applied_at) "
            + f"VALUES ('{fname}', datetime('now'));\n"
            + "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except Exception:
            conn.rollback()  # undo the partially-applied DDL (SQLite DDL is transactional)
            raise
        applied.append(f.name)
    return applied
