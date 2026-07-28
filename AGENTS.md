# Mnema Engineering Instructions

- Read `/home/jose/.codex/RTK.md`; prefix shell commands with `rtk`.
- Safety outranks convenience. Automatic source deletion remains disabled by default.
- Never add working iCloud deletion without a separate reviewed milestone.
- Never request, log, commit, or embed real credentials.
- All destructive tests use temporary directories and require `MNEMA_ALLOW_TEST_DELETE=1`.
- Source deletion requires `DeletionSafetyGate`; routes and adapters cannot bypass it.
- State changes use domain transitions and immutable audit events in one transaction.
- External operations must be idempotent and independently verified.
- Stream file data; never read entire archive objects into memory.
- Validate all paths remain beneath configured roots; reject symlinks.
- Use filesystem UUID/device identity, never stable assumptions about `/dev/sdX`.
- Use subprocess argument arrays, never `shell=True` or concatenated command strings.
- Preserve user changes. Use `apply_patch` for manual file edits.
- Required checks before handoff: `ruff`, `mypy`, `pytest`.
- Do not claim Docker, ARM64, Kopia, MinIO, rclone, or browser behavior without running it.

