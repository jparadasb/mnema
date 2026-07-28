# Mnema Implementation Plan

## Scope

Initial delivery establishes an installable, testable appliance skeleton and a complete local-source archive proof. It does not implement Apple authentication, iCloud transfer, or production deletion.

## Assumptions

- Python 3.12+ is available in production images; developer environments may use a compatible newer Python.
- Active and backup paths are preformatted and mounted by the operator or OpenMediaVault.
- Kopia and rclone are mature process boundaries. Test doubles are allowed in fast tests; executable integration tests require their binaries.
- S3-compatible MinIO is test cold storage. Client-side encryption is performed before upload.
- One appliance and one SQLite writer-heavy worker are expected.
- Default concurrency is one; configured maximum is two.

## Milestones

### M0: repository foundation

Package layout, pinned production dependencies, lint/type/test tooling, migrations, container build, Compose, CI, docs, ADRs, security policy, and dependency notices.

Exit: clean install; `ruff`, `mypy`, and unit tests pass; application imports.

### M1: appliance skeleton

Safe ARM64/Debian preflight, disk inventory by stable identity, secrets, directories, Compose/systemd installation skeleton, onboarding token, diagnostics and recovery scripts. No disk formatting.

Exit: unsupported or unsafe hosts fail closed; dry-run/preflight is testable without root mutations.

### M2: local archive vertical slice

Persistent state machine, durable queue, local source, streaming staging and atomic commit, Kopia boundary, encrypted S3 boundary, quarantine, safety gate, test-only deletion, receipts, restore verification, web/CLI, startup recovery.

Exit: temporary source completes workflow; missing receipt, changed source, same device, pause, or unelapsed quarantine blocks deletion; local and remote restore hashes match.

## Delivery boundaries

Included:

- Local filesystem source only.
- MinIO-compatible test remote.
- SFTPGo and optional cloudflared Compose services.
- Lightweight English/Spanish administration shell.
- Manual SFTPGo initial provisioning.

Deferred:

- iCloud Drive/Photos adapters.
- Production source deletion.
- OpenMediaVault API integration.
- SFTPGo API provisioning.
- Cloudflare Access JWT validation.
- Automated disk partitioning or formatting.
- Offline dependency bundle.

## Work sequence

1. Establish domain invariants and persistence.
2. Add adapters behind typed protocols.
3. Implement workflow and independent verification.
4. Add CLI and web boundary.
5. Add installer/deployment assets.
6. Test failure recovery and destructive guards.
7. Measure resource use and verify ARM64 container build where tooling permits.

## Dependency and license review

Runtime libraries are permissively licensed: FastAPI (MIT), Pydantic (MIT), SQLAlchemy (MIT), Alembic (MIT), Jinja2 (BSD-3-Clause), Uvicorn (BSD-3-Clause), Typer (MIT), cryptography (Apache-2.0/BSD), boto3/botocore (Apache-2.0), python-multipart (Apache-2.0), and PyYAML (MIT). Developer tools are not redistributed in the runtime artifact.

External services have separate terms/licenses: Kopia (Apache-2.0), rclone (MIT), SFTPGo (AGPL-3.0), MinIO server (AGPL-3.0), cloudflared (Apache-2.0), and OpenMediaVault (GPL-3.0). Mnema communicates with these as separate processes/services and does not copy their source into this repository. Before binary redistribution, release engineering must repeat license and notice review for exact shipped versions.

`THIRD_PARTY_NOTICES.md` records declared versions and licenses. CI checks dependency metadata but is not legal advice.

## Unresolved risks

- Apple APIs may not support durable stable identity or deletion confirmation uniformly.
- USB/SATA bridge serial and filesystem UUID visibility varies by HAT firmware.
- Device identity from `st_dev` is sufficient for mounted-path fail-closed checks but not proof of separate physical media through every storage stack.
- Kopia snapshot verification semantics and cold-provider restore delay need provider-specific validation.
- Container memory varies by architecture and SFTPGo/MinIO versions.
- Power-loss behavior needs physical Pi fault-injection tests.

