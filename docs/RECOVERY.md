# Recovery

## Power loss or restart

Startup checks storage existence/writability, device separation, SQLite integrity,
host-collected SMART health, expired leases, and `.partial` inventory. Durable checkpoints
around external operations let startup distinguish retryable transfer/backup/upload states
from ambiguous deletion. `DELETING` is committed before the source call and enters manual
review after restart because process disappearance is not proof of external completion.
Deletion stays paused until explicit healthy resume.

`mnema-smart.timer` refreshes `/var/lib/mnema/smart-health.json` every 15 minutes. Missing, malformed, or failed SMART reports block worker startup on an installed appliance.

Abandoned `.partial` files are reported. Current workflow deletes only exact item partial before retry; it never recursively removes directories.

## Restore

Restore local version first through Kopia boundary, then hash plaintext. Remote restore downloads encrypted object, authenticates/decrypts, then hashes plaintext. Mismatch fails verification.

## Database loss

Stop services, preserve all storage, restore config/DB backup, run `PRAGMA integrity_check`, run `mnema diagnostics`, and reconcile every item. Never infer safe deletion from files alone.

## Missing disk

Stop archive/deletion. Restore original filesystem by UUID. Do not replace mount with empty directory. Diagnostics and guard fail closed while either destination is absent.
