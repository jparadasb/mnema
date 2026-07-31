# Mnema Architecture

## Purpose

Mnema Core coordinates existing storage tools. OpenMediaVault owns disks and SMB, SFTPGo owns file protocols and portal, Kopia owns versioned backup format, rclone/object APIs own remote transport, and Cloudflare optionally owns edge access.

Mnema owns policy, state, verification, safety, restore orchestration, health, and audit.

## Components

- **API/web process:** FastAPI, Jinja2, HTMX-compatible HTML, session/CSRF/CSP boundary.
- **Public API/web process:** optional unexposed Compose service validates Cloudflare
  Access JWT signature, issuer, audience, expiry, issue time, and subject before the
  normal session/CSRF boundary. Local emergency web remains a separate service.
- **Worker:** leases durable jobs, runs at concurrency one by default, executes idempotent workflow steps.
- **SQLite:** archive items, receipts, jobs, worker heartbeats, settings, and append-only audit events. WAL mode and foreign keys enabled.
- **Mnema Bridge:** source protocol, local test adapter, and read-only iCloud Photos
  importer. `icloudpd` copies originals into a protected active namespace; Mnema then
  adopts, hashes, backs up, encrypts, and restore-verifies them.
- **Mnema Vault:** active storage. Writes use `.partial`, streaming SHA-256, fsync, atomic replace, and destination-directory fsync.
- **Mnema Archive:** Kopia backup and client-side encrypted cold copy transported through
  direct S3 or selectable rclone process boundary. Scaleway mode verifies the Standard
  object before an idempotent, independently observed transition to Glacier; retrieval
  uses the provider's asynchronous restore operation.
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
9. Provider-specific archival runs only after verification; Scaleway must report `GLACIER`.
10. Item enters quarantine.
11. Guard checks every receipt, health condition, pause, device separation, limits, quarantine, and current source metadata.
11. Test-only deletion uses expected version, then confirms absence. Ambiguity enters manual review.
12. Tombstone and audit history remain permanent.

## Persistence

SQLAlchemy 2 models use SQLite. State transitions validate against a central graph,
update state, and insert an immutable audit row in the same transaction. Archive
checkpoints commit before and after external download, backup, upload, verification, and
deletion calls. No SQLite write transaction remains open while waiting for external I/O,
and `DELETING` is durable before the source call begins. SQLite still serializes writes;
concurrency one is default, while concurrency two passed the disposable 10,000-file
stress run.

Jobs use `available_at`, `lease_owner`, `lease_expires_at`, attempts, maximum attempts, heartbeat timestamps, and unique idempotency keys. Expired running jobs return to pending during startup recovery. Backoff is capped exponential.

## Trust boundaries

- Untrusted: source filenames/content, browser requests, remote metadata, adapter output, environment variables.
- Privileged host installer: user creation, directories, systemd, Docker. It validates before mutation.
- Containers receive only required mounts. No Docker socket. Active storage is mounted
  into SFTPGo; backup/config/secrets are not. Bind mounts set
  `create_host_path: false`, so a missing UUID mount cannot silently become a directory
  on the system disk. Installer also adds `RequiresMountsFor` gates to Docker and Mnema
  systemd units, plus explicit mountpoint checks before Compose starts.
- Secrets are files or runtime environment values outside source control.

## Startup and power recovery

Startup validates mounted writable roots, device separation, SQLite integrity, and remote/backup health; recovers expired leases; inventories `.partial` files without recursive deletion; reconciles interrupted states; and moves uncertain `DELETING` items to manual review. Deletion starts paused and is enabled only by explicit action after health passes.

## Web surface

Server-rendered, mobile-first pages provide setup, status, policy preview, queue/audit inspection, restores, and emergency pause. No general file browser. English and Spanish strings are keyed in package data.

## Deployment

One image runs `mnema web` or `mnema worker`. A host CLI owns typed desired state,
atomic configuration, lifecycle, backup/restore, update, and rollback. Compose includes
Mnema, SFTPGo, MinIO test profile, and optional cloudflared/public-web profile.
Production expects host-managed UUID mounts. systemd supervises Compose.
