SQLite Schema Migration Ordering

**Rule**: Indexes on new columns added to existing tables must go in migration (after ALTER TABLE), not in SCHEMA_SQL.

**Why**: SCHEMA_SQL runs as a single `executescript()`. For existing tables, `CREATE TABLE IF NOT EXISTS` is a no-op, but `CREATE INDEX` on columns that don't exist yet fails with `OperationalError: no such column`. The ALTER TABLE migration runs after executescript, so indexes in migration are safe.

**How to apply**: Any `CREATE INDEX` that references a newly-added column must be in the `_migrate_*()` function, not in SCHEMA_SQL. Only indexes on columns that exist in the original schema should be in SCHEMA_SQL.