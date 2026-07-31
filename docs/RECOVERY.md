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

An interrupted iCloud client run never starts Mnema adoption. Re-run
`sudo mnema icloud auth` when status reports expired authentication, then
`sudo mnema icloud sync`. Existing active, Kopia, and encrypted cold copies remain intact.

## Restore

Restore local version first through Kopia boundary, then hash plaintext. Remote restore downloads encrypted object, authenticates/decrypts, then hashes plaintext. Mismatch fails verification.

## Database loss

Stop services, preserve all storage, restore config/DB backup, run `PRAGMA integrity_check`, run `mnema diagnostics`, and reconcile every item. Never infer safe deletion from files alone.

## Missing disk

Stop archive/deletion. Restore original filesystem by UUID. Do not replace mount with
empty directory. Installed systemd gates prevent Docker and Mnema from starting until
both storage paths are actual mounts. Diagnostics and guard fail closed while either
destination is absent.

## Controlled physical power-loss drill

Run only with someone physically beside the Raspberry Pi. The drill creates a disposable
source, database, Kopia repository, MinIO instance, credentials, and 5 GiB object beneath
a dedicated workspace. It mounts no production Mnema data and never calls source deletion.

```bash
sudo install -d -m 0750 /srv/mnema-backup/mnema-power-loss-workspace
sudo scripts/prepare-power-loss-test.sh /srv/mnema-backup/mnema-power-loss-workspace \
  --acknowledge-power-cut
```

Wait for `POWER-LOSS TEST READY`, then physically disconnect power. Normal shutdown or
`reboot` does not prove sudden-power-loss recovery and causes test cleanup. Reconnect
power, wait for UUID mounts and Docker, then run:

```bash
sudo scripts/recover-power-loss-test.sh /srv/mnema-backup/mnema-power-loss-workspace
```

Recovery refuses success unless Linux boot ID changed. It requires the durable item to
reconcile from `COLD_UPLOAD_PENDING` to `FAILED_RETRYABLE`, retry to quarantine, leave
one snapshot/object and no incomplete multipart upload, independently restore both
copies, and retain SQLite integrity. Successful recovery removes all drill resources.
