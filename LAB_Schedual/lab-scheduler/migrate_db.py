#!/usr/bin/env python3
"""
One-time migration script for the Lab Scheduler update package.

What it does (in order), and ONLY that:
  1. Backs up the existing SQLite file to <db>.bak-<timestamp> before
     touching anything.
  2. Creates the new `admin_credentials` table (if it doesn't already
     exist) and seeds it with a hashed password, using the ADMIN_USERNAME /
     ADMIN_PASSWORD environment variables that are already set on your
     running container as the initial GUI login.
  3. Removes the `priority` column from `bookings`, preserving every
     existing row and all other columns, using SQLite's standard
     "rebuild" pattern (works on any SQLite version — no dependency on
     ALTER TABLE ... DROP COLUMN, which needs SQLite >= 3.35).

Safe to re-run: every step checks the current schema/data state first
and is a no-op if that step was already applied. It does not use
SQLAlchemy or import anything from the app package, so it can be run
against the OLD container/image before you deploy the new code.

Usage (inside the container):
    python migrate_db.py
"""
import hashlib
import os
import shutil
import sqlite3
import sys
from datetime import datetime

PBKDF2_ITERATIONS = 260_000


# ---------------------------------------------------------------------------
# Password hashing — duplicated (not imported) from app/security.py on
# purpose, so this script has zero dependency on the new app code and can
# run safely against your currently-deployed container.
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def resolve_db_path() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:////app/data/lab_scheduler.db")
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise SystemExit(f"This migration script only supports SQLite URLs. Got: {url}")
    return url[len(prefix):]


def backup(db_path: str) -> str:
    if not os.path.exists(db_path):
        raise SystemExit(f"Database file not found at {db_path}. Nothing to migrate.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.bak-{stamp}"
    shutil.copy2(db_path, backup_path)
    print(f"[1/3] Backed up database to: {backup_path}")
    return backup_path


def ensure_admin_credentials_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(120) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM admin_credentials")
    (count,) = cur.fetchone()
    if count > 0:
        print("[2/3] admin_credentials already has data — leaving it as is.")
        return

    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "changeme")
    pw_hash = hash_password(password)
    cur.execute(
        "INSERT INTO admin_credentials (username, password_hash) VALUES (?, ?)",
        (username, pw_hash),
    )
    conn.commit()
    print(
        f"[2/3] Seeded admin_credentials with username '{username}' "
        f"(password copied from your current ADMIN_PASSWORD env var). "
        f"You can change both from the GUI at /admin/settings after the update."
    )


def remove_priority_column(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(bookings)")
    columns = [row[1] for row in cur.fetchall()]

    if "priority" not in columns:
        print("[3/3] `priority` column already absent from bookings — nothing to do.")
        return

    print("[3/3] Removing `priority` column from bookings (rebuilding table)...")

    cur.execute("SELECT COUNT(*) FROM bookings")
    (old_count,) = cur.fetchone()

    cur.execute(
        """
        CREATE TABLE bookings_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name VARCHAR(120) NOT NULL,
            equipment VARCHAR(120) NOT NULL,
            experiment_details TEXT,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            admin_note TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        INSERT INTO bookings_new (
            id, user_name, equipment, experiment_details,
            start_date, end_date, status, admin_note, created_at, updated_at
        )
        SELECT
            id, user_name, equipment, experiment_details,
            start_date, end_date, status, admin_note, created_at, updated_at
        FROM bookings
        """
    )

    cur.execute("SELECT COUNT(*) FROM bookings_new")
    (new_count,) = cur.fetchone()

    if old_count != new_count:
        conn.rollback()
        raise SystemExit(
            f"ABORTING: row count mismatch during migration ({old_count} -> {new_count}). "
            f"No changes were committed — your original `bookings` table is untouched. "
            f"Please report this before re-running."
        )

    cur.execute("DROP TABLE bookings")
    cur.execute("ALTER TABLE bookings_new RENAME TO bookings")

    # Recreate the indexes the ORM expects (lost when the table was rebuilt)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_bookings_equipment ON bookings (equipment)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_bookings_start_date ON bookings (start_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_bookings_end_date ON bookings (end_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings (status)")

    conn.commit()
    print(f"      Migrated {new_count} booking rows successfully. `priority` column removed.")


def main() -> None:
    db_path = resolve_db_path()
    print(f"Using database file: {db_path}")

    backup(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        ensure_admin_credentials_table(conn)
        remove_priority_column(conn)
    finally:
        conn.close()

    print("\nMigration complete. A backup of your pre-migration database was saved next to it.")
    print("You can now deploy the updated application code.")


if __name__ == "__main__":
    main()
