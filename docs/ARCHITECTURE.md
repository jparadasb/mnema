# Mnema Architecture

## Purpose

Mnema Core coordinates existing storage tools. OpenMediaVault owns disks and SMB, SFTPGo owns file protocols and portal, Kopia owns versioned backup format, rclone/object APIs own remote transport, and Cloudflare optionally owns edge access.

Mnema owns policy, state, verification, safety, restore orchestration, health, and audit.

## Components

- **API/web process:** FastAPI, Jinja2, HTMX-compatible HTML, session/CSRF/CSP boundary.
- **Worker:** leases durable jobs, runs at concurrency one by default, executes idempotent workflow steps.
- **SQLite:** archive items, receipts, jobs, worker heartbeats, settings, and append-only audit events. WAL mode and foreign keys enabled.
- **Mnema Bridge:** source protocol plus local test adapter. iCloud classes fail with `NotImplementedError`.
- **Mnema Vault:** active storage. Writes use `.partial`, streaming SHA-256, fsync, atomic replace, and destination-directory fsync.
- **Mnema Archive:** Kopia backup and encrypted S3-compatible cold copy.
- **Mnema Guard:** pure deletion prerequisites plus immediate source revalidation and transactional state changes.

## Archive flow

1. Discovery records stable source identity and immutable metadata snapshot.
2. Policy marks item eligible or ineligible.
3. Queue leases workflow by idempotency key.
4. Adapter copies into staging while hashing; source metadata is checked again.
5. File and staging directory are fsynced; file is atomically renamed under active root; destination directory is fsynced.
6. Active copy is independently hashed.
7. Kopia creates snapshot; restore-to-temporary-file verification must match plaintext hash.
8. Cold adapter encrypts content with authenticated encryption, uploads deterministic object key, downloads/decrypts to a temporary file, and verifies plaintext hash.
9. Item enters quarantine.
10. Guard checks every receipt, health condition, pause, device separation, limits, quarantine, and current source metadata.
11. Test-only deletion uses expected version, then confirms absence. Ambiguity enters manual review.
12. Tombstone and audit history remain permanent.

## Persistence

SQLAlchemy 2 models use SQLite. State transitions lock the transaction logically, validate against a central transition graph, update state, and insert an immutable audit row before commit. SQLite serializes writes; one worker minimizes contention.

Jobs use `available_at`, `lease_owner`, `lease_expires_at`, attempts, maximum attempts, heartbeat timestamps, and unique idempotency keys. Expired running jobs return to pending during startup recovery. Backoff is capped exponential.

## Trust boundaries

- Untrusted: source filenames/content, browser requests, remote metadata, adapter output, environment variables.
- Privileged host installer: user creation, directories, systemd, Docker. It validates before mutation.
- Containers receive only required mounts. No Docker socket. Active storage is mounted into SFTPGo; backup/config/secrets are not.
- Secrets are files or runtime environment values outside source control.

## Startup and power recovery

Startup validates mounted writable roots, device separation, SQLite integrity, and remote/backup health; recovers expired leases; inventories `.partial` files without recursive deletion; reconciles interrupted states; and moves uncertain `DELETING` items to manual review. Deletion starts paused and is enabled only by explicit action after health passes.

## Web surface

Server-rendered, mobile-first pages provide setup, status, policy preview, queue/audit inspection, restores, and emergency pause. No general file browser. English and Spanish strings are keyed in package data.

## Deployment

One image runs `mnema web` or `mnema worker`. Compose includes Mnema, SFTPGo, MinIO test profile, and optional cloudflared profile. Production expects host-managed UUID mounts. systemd supervises Compose.

