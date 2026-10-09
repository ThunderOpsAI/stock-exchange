"""
Ordered, Idempotent SQLite Migration Engine.
Adheres strictly to ADR 0003 and SPEC.md Phase 2.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationEngine:
    def __init__(self, db_path: str, migrations_dir: Optional[Path] = None):
        self.db_path = db_path
        self.migrations_dir = migrations_dir or MIGRATIONS_DIR

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def ensure_migration_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()

    def discover_migrations(self) -> List[Tuple[int, str, Path]]:
        """
        Discovers all .sql migration files in the migrations directory.
        Files must follow pattern: <NNNN>_<name>.sql (e.g. 0001_initial_schema.sql)
        Returns list of (version, name, file_path) sorted by version ascending.
        """
        if not self.migrations_dir.exists():
            return []

        migrations: List[Tuple[int, str, Path]] = []
        pattern = re.compile(r"^(\d+)[-_](.+)\.sql$")

        for file in self.migrations_dir.glob("*.sql"):
            match = pattern.match(file.name)
            if match:
                version = int(match.group(1))
                name = match.group(2)
                migrations.append((version, name, file))

        migrations.sort(key=lambda x: x[0])
        return migrations

    def get_applied_versions(self, conn: sqlite3.Connection) -> List[int]:
        self.ensure_migration_table(conn)
        cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
        return [row[0] for row in cursor.fetchall()]

    def bootstrap_legacy_database(self, conn: sqlite3.Connection) -> None:
        """
        Handles backward compatibility for existing databases that were created
        using the legacy schema.sql without a schema_migrations table.
        If existing tables (e.g. market_snapshots or orders) exist, marks migration 1 as applied.
        """
        self.ensure_migration_table(conn)
        applied = self.get_applied_versions(conn)
        if 1 not in applied:
            # Check if legacy tables exist
            cursor = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('market_snapshots', 'orders')"
            )
            count = cursor.fetchone()[0]
            if count > 0:
                logger.info("Existing database detected without migration history; bootstrapping migration 1")
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
                    (1, "initial_schema"),
                )
                conn.commit()

    def run_migrations(self) -> List[int]:
        """
        Executes all unapplied migrations in sequential order.
        Each migration is applied inside an atomic transaction.
        Returns list of newly applied migration versions.
        """
        conn = self.get_connection()
        applied_now: List[int] = []

        try:
            self.ensure_migration_table(conn)
            self.bootstrap_legacy_database(conn)

            applied_versions = set(self.get_applied_versions(conn))
            all_migrations = self.discover_migrations()

            for version, name, file_path in all_migrations:
                if version in applied_versions:
                    continue

                logger.info(f"Applying SQLite migration {version:04d}_{name}...")
                with open(file_path, "r", encoding="utf-8") as f:
                    migration_sql = f.read()

                # Execute script atomically
                conn.executescript(migration_sql)

                # Record migration record
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (version, f"{version:04d}_{name}"),
                )
                conn.commit()
                applied_now.append(version)
                logger.info(f"Migration {version:04d}_{name} applied successfully.")

            return applied_now

        finally:
            conn.close()
