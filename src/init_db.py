"""Apply all migrations to the local SQLite DB.

Idempotent — migrations are tracked in _schema_migrations, so re-running is a
no-op when the schema is already up to date.

    python3 src/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python src/init_db.py` from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.io import DB_PATH, MIGRATIONS_DIR, apply_migrations, connect


def main() -> int:
    print(f"DB:         {DB_PATH}")
    print(f"Migrations: {MIGRATIONS_DIR}")
    conn = connect(DB_PATH)
    try:
        applied = apply_migrations(conn, MIGRATIONS_DIR)
        for name in applied:
            print(f"  applied {name}")
        if not applied:
            print("  (all migrations already applied)")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name")]
        print(f"\nTables now in DB ({len(tables)}):")
        for t in tables:
            print(f"  {t}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
