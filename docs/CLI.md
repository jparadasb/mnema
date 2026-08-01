# Mnema appliance CLI

Mnema has one host-facing command for installation, configuration, lifecycle, diagnostics,
backup, update, rollback, and safe runtime removal. Runtime archive commands use the same
entry point inside containers.

## Bootstrap

From a reviewed Mnema release on the Raspberry Pi:

```bash
sudo ./scripts/bootstrap-cli.sh
sudo mnema install --source-root "$PWD"
```

Bootstrap creates a private environment under `/opt/mnema-cli` and links
`/usr/local/bin/mnema`. Installation validates ARM64, Docker, mounted storage UUIDs,
separation, permissions, SMART support, and Compose before starting services. It never
formats disks and leaves deletion disabled.

## Guided configuration

Run the full wizard:

```bash
sudo mnema configure
```

Or configure one boundary:

```bash
sudo mnema configure storage
sudo mnema configure cold-storage
sudo mnema configure cloudflare
sudo mnema configure file-provider
sudo mnema configure icloud
sudo mnema configure sftpgo
sudo mnema configure policy
```

Non-secret desired state lives at `/etc/mnema/config.yaml`. Secrets remain individual
root-owned files beneath `/etc/mnema/secrets`. `mnema config show` always redacts secret
locations. Each apply shows a redacted preview, validates storage and Compose, writes
atomically, restarts affected services, checks health, and restores prior configuration
on failure.

Automation supplies secret file paths, never secret values:

```bash
sudo mnema configure cold-storage \
  --transport rclone \
  --remote-root provider:private/mnema \
  --rclone-config-file /safe/input/rclone.conf \
  --yes

sudo mnema configure cold-storage \
  --provider scaleway \
  --region fr-par \
  --s3-bucket YOUR_EXISTING_BUCKET \
  --s3-access-key-file /safe/input/scaleway-access-key \
  --s3-secret-key-file /safe/input/scaleway-secret-key \
  --yes

sudo mnema configure cloudflare \
  --enabled \
  --team-domain https://team.cloudflareaccess.com \
  --audience APPLICATION_AUD_TAG \
  --hostname admin.example.com \
  --token-file /safe/input/tunnel-token \
  --yes
```

The Cloudflare command configures an existing remotely managed tunnel. The operator must
create the Access application, Allow policy, and published hostname in Cloudflare. Only
Mnema administration is supported through the tunnel. SFTPGo web and raw SFTP remain
local-only.

## iPhone File Provider

File Provider requires the existing Cloudflare Tunnel plus a separate API hostname routed
to `http://file-provider-api:8082`. Administration remains protected by Cloudflare Access;
the Files API uses revocable device credentials so background synchronization does not
depend on an interactive browser session.

```bash
sudo mnema configure file-provider --enabled --public-url https://files.example.com --yes
sudo mnema file-provider project
sudo mnema file-provider pair
sudo mnema file-provider devices
sudo mnema file-provider revoke DEVICE_ID
```

`project` idempotently exposes existing fully verified archives in read-only collections.
Inbox accepts file creation only. Uploads become read-only after Kopia, remote restore/hash,
and Glacier verification. No File Provider command deletes archive content.

### Scaleway Glacier

The Scaleway provider accepts only `fr-par` or `nl-ams` and derives the official regional
S3 endpoint. The bucket must already exist. Mnema uploads an AES-256-GCM encrypted object
in Standard storage, independently downloads/decrypts/hashes it, and only then changes
the same object to `GLACIER`. Objects larger than the single-copy S3 limit use multipart
server-side copy. The transition is idempotent and must be observed with `HeadObject`
before the archive state advances.

Glacier restores are asynchronous. The first remote restore attempt submits
`RestoreObject` and records a pending audit result. Retry the restore from the Mnema
restore page after Scaleway completes retrieval. Mnema never configures lifecycle
expiration and never deletes cloud objects.

## iCloud Photos

Mnema supports one dedicated Apple account and Personal Library. It imports original
photos, videos, RAW assets, and both Live Photo components. iCloud Drive is unsupported.
Guarded Apple-side capacity relief is optional and disabled by default.

The account must have iCloud web access enabled and Advanced Data Protection disabled.
These are limitations of the pinned `icloudpd` web-access client. Prefer a dedicated
account because disabling Advanced Data Protection reduces that account's protection.

```bash
sudo mnema configure icloud
sudo mnema icloud auth
sudo mnema icloud preview
sudo mnema icloud sync
sudo mnema icloud status
sudo mnema icloud storage
```

Configuration prompts for Apple ID, password, and 2FA. Mnema stores no password or 2FA
code. Protected session cookies live under `/etc/mnema/icloud-session`. Configuration
authenticates and previews but never starts the first import. First successful explicit
`mnema icloud sync` arms the daily 03:00 local-time timer.

Each sync uses fixed `icloudpd` copy-mode arguments. Its destructive flags are not accepted
or exposed. Expired authentication fails closed; re-run `mnema icloud auth`. Disabling
iCloud stops its timer while preserving sessions and archived files.

Enable cleanup proposals explicitly:

```bash
sudo mnema configure icloud --enabled --capacity-relief
sudo mnema icloud cleanup preview
sudo mnema icloud cleanup status
sudo mnema resume-deletion
sudo mnema icloud cleanup approve MANIFEST_ID
```

`preview` creates a 24-hour immutable manifest only when fresh total iCloud usage is at
least 90%. It chooses oldest verified non-favorite assets toward 80%, capped at 1,000 assets
and 10% of quota. `approve` displays the manifest and requires its 12-character digest
prefix. Every safety fact and Apple change tag is revalidated before execution. Assets move
only to Recently Deleted; both Pi copies and Glacier remain. Any ambiguous result stops the
batch, enables the safety lock, and requires manual review.

Current build permits quota inspection and manifest review but keeps execution blocked by
the internal milestone-approval flag. Only a reviewed release produced after the dedicated
synthetic-account test may set that flag; appliance configuration does not expose it.

## Service URLs

Show configured, reachable endpoints and whether their containers are running:

```bash
mnema urls
mnema urls --json
```

With Cloudflare enabled, Mnema reports the protected internet URL and keeps the
localhost recovery URL visible. Without Cloudflare, it reports the configured local web
bind; a localhost-only bind includes SSH tunnel guidance. SFTP reports the Raspberry
Pi LAN address and port 2022. Internal SFTPGo administration and integration MinIO
ports are omitted. Failure to inspect Docker marks state as `unknown` without hiding
configured addresses.

## Lifecycle and recovery

```bash
sudo mnema startup
sudo mnema start
sudo mnema stop
sudo mnema restart
sudo mnema enable
sudo mnema disable
mnema status
mnema logs
mnema diagnostics
mnema verify
```

Configuration backups contain secrets and must be protected:

```bash
sudo mnema backup create /safe/path/mnema-config-2026-07-30.tar.gz
sudo mnema restore config /safe/path/mnema-config-2026-07-30.tar.gz
```

Install latest published GitHub release:

```bash
sudo mnema update --latest
```

For unattended maintenance, add `--yes`. Health checks and automatic rollback still apply.

Mnema requires fixed archive/checksum assets, GitHub's SHA-256 asset digest, and agreement
between both hashes. It streams downloads over validated GitHub HTTPS endpoints, then uses
the same backup, health-check, and automatic rollback path as local updates.

Offline updates accept a local archive with an independently supplied SHA-256:

```bash
sudo mnema update --archive /safe/path/mnema-release.tar.gz --sha256 HASH
sudo mnema rollback
```

Update stops services, creates a configuration/SQLite backup, preserves the old image and
application directory, installs the verified release, and rolls back automatically when
health checks fail.

`mnema uninstall` prints a plan and changes nothing. Executing it only stops and disables
runtime services:

```bash
sudo mnema uninstall --execute --confirm "$(hostname)"
```

Application state, secrets, archives, storage filesystems, and users remain untouched.
