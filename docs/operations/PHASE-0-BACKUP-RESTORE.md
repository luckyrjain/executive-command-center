# Phase 0 Backup and Restore

## Objective

Prove that the foundation PostgreSQL database can be backed up and restored into a clean local environment without data loss or cloud dependencies.

## Scope

The Phase 0 backup includes:

- migration metadata
- workspace and owner identity
- server-side sessions where present
- PKOS foundation nodes, edges, evidence and provenance
- event outbox, inbox and dead-letter records
- application configuration persisted in PostgreSQL

Local object files are out of scope until a Phase specification introduces persisted source files.

## Backup format

- logical PostgreSQL custom-format archive created with `pg_dump --format=custom`
- archive filename includes UTC timestamp and schema version
- SHA-256 checksum generated beside the archive
- archive and checksum stored under an ignored local backup directory
- Phase 0 development backups are not encrypted because they contain only synthetic test data
- backups containing real user data MUST be encrypted before leaving the local machine

## Restore procedure

1. Start a clean PostgreSQL 18 container with an empty database.
2. Verify the backup SHA-256 checksum.
3. Restore with `pg_restore` into the clean database.
4. Run schema-version and migration-head checks.
5. Run integrity queries for workspace, PKOS, event and identity records.
6. Start the application and run health and repository smoke tests.

## Recovery targets

For the Phase 0 synthetic foundation dataset:

- recovery point objective: last successful manual backup
- recovery time objective: 10 minutes on a supported developer machine
- checksum verification: mandatory
- restored row counts and referential integrity: must match the source database

These are development targets, not production service-level commitments.

## Automation

The repository MUST expose documented commands equivalent to:

```text
make backup
make restore BACKUP=<archive>
make verify-restore BACKUP=<archive>
```

The exact command names may differ, but backup, restore and verification MUST be independently executable and non-interactive in CI.

## CI validation

CI MUST:

1. apply all migrations to a clean PostgreSQL 18 database
2. load deterministic synthetic foundation fixtures
3. produce a logical backup
4. restore into a second clean database
5. verify checksum, migration head, row counts and repository smoke tests

## Failure handling

- checksum mismatch aborts restoration
- incompatible schema version aborts restoration with a clear error
- partial restoration is treated as failure
- the source database is never modified by restore verification

## Exit evidence

The Phase 0 exit review MUST include the CI artifact names, backup checksum, restore duration and integrity-test result.
