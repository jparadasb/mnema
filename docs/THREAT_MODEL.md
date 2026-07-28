# Mnema Threat Model

## Assets

Personal files, plaintext hashes and metadata, credentials, encryption keys, administrator session, audit history, deletion authority, active storage, backup repository, and cold archive.

## Security objectives

Prevent unauthorized reads and deletes; never delete a source before independently verified copies; make destructive actions attributable; recover conservatively from interruption; fail closed when identity or health is uncertain.

## Threats and controls

- **Stolen Pi/SSD:** cold copy uses client-side authenticated encryption; recommend host/full-disk encryption; secrets mode `0600`; document physical exposure. Active NAS encryption remains operator responsibility.
- **Compromised NAS account/ransomware:** SFTPGo sees active data only, never backup repository or secrets; Kopia versions and remote copy provide recovery; least-privilege users.
- **Compromised Apple/source account:** source credentials scoped when future APIs allow; deletion disabled by default; per-run caps, pause, revalidation, and audit.
- **Compromised Cloudflare account:** tunnel optional; local emergency access retained; future Access JWT must validate issuer, audience, signature, and expiry.
- **Compromised object credentials:** client-side encryption limits disclosure; scoped bucket credentials; immutable/object-lock policy recommended; credentials cannot enable source deletion alone.
- **Malicious files/path traversal/symlinks:** resolved containment checks, relative-path normalization, symlink rejection, no execution, no thumbnail/indexing pipeline.
- **Command injection:** subprocess argument arrays, validated identifiers, no `shell=True`.
- **Accidental mass deletion:** global and per-source off by default, quarantine, manual approval option, item/byte/percentage limits, one-at-a-time deletion, permanent tombstones.
- **Backup corruption:** independent restore-and-hash verification; success exit status alone is insufficient; periodic restore tests.
- **Power failure:** fsync/atomic rename, transactional state/audit, leases, startup reconciliation, uncertain deletion enters manual review.
- **Supply-chain compromise:** pinned Python/container versions, hashes/lockfile planned, Buildx/CI scans planned, notices, minimal image, routine updates.
- **Secret leakage:** redaction, secret files outside repository, secure generation, no credentials in commands/logs, restrictive permissions.
- **Container escape:** non-root user where practical, dropped capabilities, `no-new-privileges`, read-only root filesystem/tmpfs, narrow mounts, no Docker socket.
- **Unauthorized LAN access:** onboarding token, administrator login, secure cookies in production, CSRF tokens, CSP, bind configuration, firewall guidance.
- **Compromised application/admin:** immutable audit intent, confirmation for destructive operations, backup isolation. Host-root compromise remains out of scope.

## Required deletion invariant

Deletion is permitted only when local path/size/hash verify, Kopia and remote restore verification receipts exist, quarantine elapsed, source identity/version is unchanged, active and backup health pass, devices differ, SQLite integrity passes, remote is available, global and source toggles are enabled, run caps pass, and no safety lock exists.

Any missing, stale, ambiguous, or contradictory fact denies deletion.

## Residual risks

- Host root can alter application, DB, mounts, and keys.
- Without disk encryption, stolen active/backup media exposes data.
- A common-mode application bug may affect receipts; physical restore drills remain necessary.
- Filesystem/device abstractions may hide shared physical failure domains.
- MinIO in same Compose host demonstrates protocol, not geographic disaster recovery.
- SFTPGo and MinIO AGPL services create operational update obligations when distributed.

## Review triggers

Review before iCloud work, production deletion, public exposure, automated formatting, new privileged mounts, new source adapter, encryption-key rotation, or binary appliance distribution.

