# Mnema

> Your cloud, remembered.

Mnema is an open-source personal cloud archiving appliance for Raspberry Pi 5. It coordinates safe movement from a source into active NAS storage, a versioned local backup, and an encrypted off-site copy. Source deletion is gated by independent verification and is disabled by default.

## What Mnema does

- Runs a persistent archive state machine and durable SQLite job queue.
- Streams local test-source files through staging, SHA-256 verification, and atomic commit.
- Integrates behind boundaries with Kopia, S3-compatible storage, SFTPGo, and future OpenMediaVault/rclone connectors.
- Encrypts cold objects client-side and verifies them by restore, decrypt, and plaintext hash.
- Provides CLI, mobile-first administration shell, audit events, quarantine, restore tests, and safe startup checks.

## What Mnema does not do

- No working iCloud Drive or iCloud Photos integration yet.
- No production source deletion.
- No custom SMB, SFTP, WebDAV, file browser, backup format, OAuth provider, RAID manager, or photo manager.
- No automatic disk partitioning or formatting.

## Hardware and base OS

Target: Raspberry Pi 5, ARM64, 4 GB RAM, Ethernet, Radxa Penta SATA HAT, and two SATA SSDs. Supported installer base: Debian ARM64 or Raspberry Pi OS Lite 64-bit. OpenMediaVault-compatible Debian installations are intended.

Default storage:

- SSD 1: active NAS data.
- SSD 2: Kopia versioned repository.
- Remote provider: encrypted cold archive.

RAID is not configured by default. Filesystem UUIDs and mounted-device identity are used; `/dev/sda` names are never persisted.

## Important warnings

- Synchronization is not backup.
- RAID is not backup.
- Automatic deletion is disabled by default.
- Successful upload is insufficient without independent verification.
- iCloud Drive and iCloud Photos expose different capabilities.
- Cold-storage restoration may be delayed.
- MinIO on the same host proves protocol behavior, not off-site resilience.

## Local development

Requires Python 3.12+.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m playwright install chromium
ruff check .
ruff format --check .
mypy src
pytest
mnema test-vertical-slice
python scripts/stress-test.py --smoke --mode all --concurrency 2
```

Run web:

```bash
MNEMA_DATABASE_URL=sqlite:///./dev.db \
MNEMA_SECRET_KEY=development-only-change-me \
mnema web --host 127.0.0.1 --port 8080
```

## Docker Compose

Create local directories and secret files first:

```bash
mkdir -p data/{active,backup,staging,test-source} secrets
openssl rand -base64 48 > secrets/mnema_secret_key
openssl rand 32 > secrets/mnema_cold_key
cp deploy/compose/.env.example .env
docker compose up --build
```

MinIO is opt-in:

```bash
docker compose --profile integration up --build
```

SFTPGo binds to localhost and receives only active-storage mount. Backup repository, application config, and secrets are not mounted into SFTPGo. Initial user provisioning is manual in this milestone.

## Appliance installation

Preformat and mount two distinct filesystems. Then:

```bash
sudo env \
  MNEMA_ACTIVE_ROOT=/srv/mnema-active \
  MNEMA_BACKUP_ROOT=/srv/mnema-backup \
  ./scripts/install.sh
```

Installer refuses non-ARM64 systems, unsupported OS, low RAM/disk, missing Docker/Compose, absent UUIDs, same storage filesystem, or unwritable mounts. It never formats disks.

## CLI

```text
mnema status
mnema scan
mnema scan --archive
mnema dry-run
mnema pause-deletion
mnema resume-deletion
mnema verify
mnema diagnostics
```

`resume-deletion` only opens global gate after startup health checks. Per-source setting, quarantine, receipts, limits, current source metadata, and all other guard checks still apply.

## Architecture, security, storage, and recovery

- [Architecture](docs/ARCHITECTURE.md)
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Storage](docs/STORAGE.md)
- [Recovery](docs/RECOVERY.md)
- [Stress testing](docs/STRESS_TESTING.md)
- [Installation](docs/INSTALLATION.md)
- [Security policy](SECURITY.md)

## Known limitations

Kopia, MinIO, Docker/Compose, ARM64 image, hardware power-loss behavior, and resource targets require executable infrastructure validation. Browser E2E uses Playwright and disposable local filesystems. Fast tests use isolated local implementations for versioned and encrypted backup boundaries. See [implementation status](docs/IMPLEMENTATION_STATUS.md).

## Roadmap

Next milestones: physical Pi validation; exact Kopia/MinIO/rclone integration; SFTPGo API provisioning; OpenMediaVault adapter; Cloudflare Access JWT validation; then separately reviewed, capability-driven iCloud connectors. Production deletion remains a later explicit milestone.

Mnema is not affiliated with Apple, Cloudflare, SFTPGo, Kopia, rclone, MinIO, or OpenMediaVault.
