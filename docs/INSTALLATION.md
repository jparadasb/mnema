# Installation

## Before installation

Use Raspberry Pi OS Lite 64-bit or Debian ARM64. Update firmware and OS, attach both SSDs, format/mount them through OpenMediaVault or audited manual steps, and record model, serial, filesystem UUID, and mount point.

Do not use real personal data during initial validation. Docker Engine and Compose plugin must be installed and running.

## Install

```bash
sudo env \
  MNEMA_ACTIVE_ROOT=/srv/mnema-active \
  MNEMA_BACKUP_ROOT=/srv/mnema-backup \
  MNEMA_SOURCE_ROOT=/srv/mnema-test-source \
  ./scripts/install.sh
```

Installer inventories disks and fails before Mnema mutation when architecture, OS, RAM, free space, Docker, UUID, mount separation, or writability is unsafe. It does not format disks.

Store one-time token offline. Visit local setup URL. Local emergency access remains available.

## SFTPGo

Encrypted SFTP is exposed to the LAN on port `2022`. WebAdmin/WebClient remains bound to host localhost port `8081` until HTTPS or Cloudflare Access is configured. Mnema bootstraps a separate SFTPGo administrator, scoped API key, and NAS user without reusing the Mnema administrator password. SFTPGo sees active storage at `/srv/mnema-active`; backup storage and Mnema configuration are not mounted.
