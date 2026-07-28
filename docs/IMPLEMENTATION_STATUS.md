# Implementation status

## Implemented

Repository/docs, typed source protocol, local source, central state graph, SQLAlchemy schema, audit transitions, leased queue, policy and deletion gate, safe path validation, streaming copy/hash/fsync/atomic commit, Kopia versioned backup, AES-GCM cold encryption, MinIO S3 storage, independent restore verification, quarantine, guarded test deletion, fail-closed startup recovery, CLI, web shell, Compose, systemd, and installer refusal checks.

## Mocked or incomplete

- Fast tests use filesystem/local test doubles; dedicated adapter unit tests cover Kopia command construction and encrypted S3 behavior.
- iCloud classes intentionally do not work.
- SFTPGo v2 API provisioning client and installer bootstrap create a separate service administrator, scoped API key, and NAS user. Real SFTP password authentication and byte-identical upload/download were validated on the Pi.
- OpenMediaVault/rclone adapters absent.
- Cloudflare Access JWT validation absent.
- Offline installer, safe update, uninstall, config restore, and admin reset are explicit non-success stubs.
- Setup persists administrator, separated storage, default fail-closed policy, local-source availability, and MinIO availability. Authenticated pages support policy edits, source preview/archive, receipt tables, Kopia/remote restore tests, emergency pause, jobs, audit, storage, and diagnostics.
- Playwright drives setup, login, policy editing, source preview, archive, local restore, remote restore, emergency pause, and audit verification at a 390 x 844 mobile viewport.
- No production deletion.

## Hardware validation

Native Python validation passed on Raspberry Pi 5 Model B Rev 1.0, Debian 13 ARM64, and Python 3.13.5: Ruff, strict mypy, local vertical proof, browser E2E, crash injection, and synthetic resource measurements. Current x86_64 validation passes all 43 tests, including the complete browser and stress-harness journeys, with 77% statement coverage. See `RESOURCE_USAGE.md`.

Radxa Penta SATA HAT enumeration passes after enabling external PCIe and the JMB585 32-bit DMA overlay. Controller negotiated PCIe Gen 2 x1. Two SSDs were identified independently, formatted only after explicit confirmation, and mounted using UUIDs:

- Kingston 120 GB, active: `/srv/mnema-active`, UUID `5284ec19-26b4-461f-95d3-06a5f7d480b5`.
- Western Digital 240 GB, backup: `/srv/mnema-backup`, UUID `10713742-9008-4be6-ac87-158ef4c547ba`.

Both mounts passed exact-file write checks and expose different filesystem device IDs. Persistent fstab mounts use `noatime,nosuid,nodev,noexec,nofail`.

ARM64 image build and runtime passed with Docker 26.1.5, Compose 2.26.1, Kopia 0.23.1, rclone 1.60.1, SFTPGo 2.6.6, and MinIO `RELEASE.2025-07-23T15-54-02Z`. A synthetic 8 MiB item completed the persisted workflow through a real Kopia snapshot and independent restore, encrypted MinIO upload and independent download/decrypt/hash, then entered seven-day quarantine. Its source remains present; global deletion is disabled and the safety lock is enabled.

Service restart recovery passed: expired leases recover to `RETRY`; interrupted download, backup, cold upload, and restore states become `FAILED_RETRYABLE`; ambiguous deletion becomes `MANUAL_REVIEW`; and immutable audit events record each reconciliation. Simulated missing backup storage forces global deletion off and enables the safety lock. SMART aggregation runs every 15 minutes and is mandatory for healthy worker startup. Physical power-cut fault injection and production deletion remain unverified.

Disposable Pi stress runs passed 10,000 files at concurrency one and two plus a 5 GiB
file larger than physical RAM. Peak process RSS remained below 110 MiB. Crash injection
after download, backup write, and encrypted cold upload recovered to quarantine with one
snapshot/object and independently verified restores. These tests used local protocol
adapters rather than Kopia/MinIO; see `RESOURCE_USAGE.md`.

Isolated real-adapter Pi runs passed one 1 GiB file and 1,000 files at concurrency one
and two through Kopia 0.23.1 and dedicated MinIO. Exact Kopia snapshot and MinIO object
counts matched quarantined item counts, and first/last restores passed from both copies.
Concurrency two reduced the 1,000-file duration from 1,076.620 to 573.858 seconds.
Mid-upload MinIO restart, real-adapter 5 GiB/10,000-file scale, and physical power loss
remain unverified.

SMART validation on 2026-07-28:

- Kingston active SSD: passed, 40°C, 2,235 power-on hours.
- Western Digital backup SSD: passed, 40°C, 7,446 power-on hours.

Playwright Chromium 149 on x86_64 and Raspberry Pi Chromium 150 on ARM64 both completed the isolated browser journey at 390 x 844: setup, login, policy editing, source preview, archive, local restore, remote restore, emergency pause, and audit verification. Disposable filesystems and a temporary database kept both runs separate from appliance data.

SFTPGo validation on 2026-07-28:

- LAN listener returned `SSH-2.0-SFTPGo_2.6.6` on port 2022.
- Password authentication succeeded for the provisioned NAS user.
- A synthetic 1 MiB file was uploaded, downloaded, and independently compared.
- Both copies produced SHA-256 `dbc878652ef34696f1fdc0d64e448e96b7b4e41e0dcefb10e24c33c4b82d4693`.
- The synthetic remote proof file was removed after validation.
- ED25519 host-key fingerprint: `SHA256:WFTCs5xTPGz2OZvbjBt2u1pDWaOxnBJxhaWCfmEhfcI`.
