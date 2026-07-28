# Security Policy

## Reporting

Do not open public issues for suspected vulnerabilities involving credential exposure, deletion bypass, path escape, remote code execution, or authentication bypass. Contact project maintainers privately through repository security advisories. No dedicated security mailbox exists in this bootstrap.

Include affected version, reproducible steps using synthetic data, impact, and suggested mitigation. Never include real personal files or credentials.

## Supported versions

Only the latest released minor version receives security fixes during initial development.

## Security invariants

- Source deletion is disabled by default and production deletion is not implemented.
- Test deletion requires `MNEMA_ALLOW_TEST_DELETE=1`, source policy, global gate, no safety lock, complete receipts, health, quarantine, and limits.
- Unsafe paths and symlinks are rejected.
- Secrets must remain outside source control with mode `0600`.
- No Mnema container mounts Docker socket.

See [threat model](docs/THREAT_MODEL.md).

