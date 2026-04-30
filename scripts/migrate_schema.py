"""
Idempotent schema migration for NeonDB.

Adds any columns the SQLAlchemy models declare but the live tables are missing.
Safe to re-run — every statement uses IF NOT EXISTS.
"""

import os
import sys
from sqlalchemy import create_engine, text

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


PG_URL = os.getenv("POSTGRES_URL", "")
if not PG_URL:
    print("POSTGRES_URL not set in environment.")
    sys.exit(1)


# (table, column, definition) — definition omits "ADD COLUMN" prefix
MIGRATIONS = [
    # incidents
    ("incidents", "project_id",            'UUID'),
    ("incidents", "resolved_target",       'JSON'),
    ("incidents", "status_timeline",       'JSON'),

    # remediation_audits
    ("remediation_audits", "project_id",          'UUID'),
    ("remediation_audits", "previous_values",     'JSON'),
    ("remediation_audits", "is_shadow_run",       "VARCHAR DEFAULT 'false'"),
    ("remediation_audits", "human_agreed",        'VARCHAR'),
    ("remediation_audits", "failure_reason",      'VARCHAR'),
    ("remediation_audits", "failure_root_cause",  'VARCHAR'),  # we use string column to skip enum migration
]


def column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.first() is not None


def table_exists(conn, table: str) -> bool:
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
    ), {"t": table})
    return result.first() is not None


def main():
    engine = create_engine(PG_URL)
    with engine.connect() as conn:
        added = 0
        skipped = 0
        for table, column, definition in MIGRATIONS:
            if not table_exists(conn, table):
                print(f"  [skip] table '{table}' does not exist yet (ORM will create it)")
                skipped += 1
                continue
            if column_exists(conn, table, column):
                print(f"  [skip] {table}.{column} already exists")
                skipped += 1
                continue
            sql = f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            print(f"  [add ] {sql}")
            conn.execute(text(sql))
            added += 1
        conn.commit()
        print(f"\nDone. Added {added} columns, skipped {skipped}.")


if __name__ == "__main__":
    main()
