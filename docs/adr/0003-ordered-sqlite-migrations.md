# ADR 0003: Ordered Idempotent SQLite Migrations and Schema Evolution

## Status
Accepted

## Context
The autonomous trading desk relies on a local SQLite database configured with Write-Ahead Logging (WAL) for persistent domain storage (orders, positions, fills, portfolio snapshots, and audit events). 

In the initial prototype:
- Database schema initialization was managed via a monolithic `schema.sql` file executed using `executescript()`.
- There was no schema versioning mechanism or migration tracking table.
- Upgrading or evolving the schema required either destructive recreation of the database file or unversioned ad-hoc SQL statements, risking data loss or inconsistent table definitions across runs and developer environments.
- As the system matures into a production-grade paper-trading engine (Phases 1 and 2), durable operational tables must be introduced—including `trading_runs`, `data_health`, `operator_controls`, `order_intents`, `broker_submissions`, `reconciliation_events`, and `protection_status`.

A disciplined, reproducible, and automated schema migration strategy is necessary to support incremental schema evolution without data loss or schema drift.

## Decision
1. **Lightweight, In-Tree Migration Framework**:
   - Implement an ordered, dependency-free migration runner in `src/storage/` that automatically applies pending migrations sequentially on application startup.
2. **Ordered Migration Scripts**:
   - Migration scripts are stored in a dedicated directory (e.g., `src/storage/migrations/`) with strict numeric sequential prefixes (e.g., `0001_baseline_schema.sql`, `0002_execution_and_runs.sql`).
   - Each script contains standard, self-contained DDL/DML statements.
3. **Schema Version Tracking Table**:
   - A dedicated tracking table `schema_migrations` records applied migrations:
     ```sql
     CREATE TABLE IF NOT EXISTS schema_migrations (
         version INTEGER PRIMARY KEY,
         name TEXT NOT NULL,
         applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
     );
     ```
   - On initialization, the runner queries `schema_migrations` and executes only unapplied migration scripts in ascending version order.
4. **Idempotency and Atomic Transactions**:
   - Each migration file is executed within an explicit database transaction (`BEGIN IMMEDIATE TRANSACTION;` ... `COMMIT;`).
   - If any migration fails, the transaction rolls back cleanly, the migration version is not recorded, and the application halts immediately with a descriptive error.
   - Running the migration runner against an up-to-date database is an idempotent no-op.
5. **SQLite Pragmas and Concurrency Invariants**:
   - Every connection enforces WAL journal mode (`PRAGMA journal_mode=WAL;`), foreign key constraints (`PRAGMA foreign_keys=ON;`), and appropriate busy timeouts (`PRAGMA busy_timeout=5000;`).
6. **Forward Compatibility & Schema Evolution Rules**:
   - Additive schema modifications (e.g., adding columns with nullable or default values, creating new tables/indexes) are prioritized.
   - Destructive modifications (dropping columns or tables) are disallowed in single-step migrations and must follow a multi-step deprecation cycle to ensure forward and backward compatibility.

## Alternatives Considered
- **Monolithic `schema.sql` with `CREATE TABLE IF NOT EXISTS`**: Retaining a single initialization script was considered. This was rejected because `CREATE TABLE IF NOT EXISTS` cannot manage column additions (`ALTER TABLE`), column alterations, indexing updates, or structured data transformations across existing databases.
- **External Heavyweight Migration Tools (e.g., Alembic / Flyway / Liquibase)**: Adopting an external tool or ORM migration library was considered. This was rejected because SQLite in Python requires only minimal standard library primitives (`sqlite3`), avoiding external tool dependencies and complex operational setup.

## Consequences
### Positive
- Fully reproducible schema state across fresh installations, testing environments, and existing production paper-trading databases.
- Auditable history of database schema evolution and schema versions.
- Safe, non-destructive database evolution enabling Phase 2 durable execution and run tracking tables.
- Zero external dependencies beyond Python's standard `sqlite3` library.

### Negative
- Developers must create discrete, sequentially numbered migration files for any schema alteration rather than directly modifying a single SQL file.
- SQLite limitations on `ALTER TABLE` (such as restrictions on modifying existing column constraints) require careful multi-step migration patterns when table restructuring is necessary.

## Migration / Implementation Impact
- Existing `src/storage/schema.sql` will be codified as migration `0001_initial_schema.sql`.
- `src/storage/db.py` will be updated to execute the migration runner during database connection setup.
- Subsequent phases (e.g., P2-01) will introduce new migration scripts (`0002_execution_tables.sql`) cleanly via this mechanism.
