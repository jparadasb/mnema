# Resource usage

Raspberry Pi 5 measurements on 2026-07-27:

- 128 MiB single file: 76,208 KiB peak RSS, 1.680 seconds; SQLite 45,056 bytes.
- 1,000 files of 4 KiB: 79,376 KiB peak RSS, 8.149 seconds; SQLite 1,953,792 bytes.

Runs used Raspberry Pi 5 Model B Rev 1.0, Debian 13 ARM64, Python 3.13.5, concurrency one, local filesystem versioned backup, AES-256-GCM local cold storage, and SQLite. They prove streaming memory remains bounded in this synthetic path. They exclude SFTPGo, MinIO, Kopia, Docker, and network overhead, so do not verify the full-stack 1.2 GB target.

Earlier x86_64 WSL2 comparison: 72,856 KiB/3.034 seconds for the large-file case and 76,236 KiB/28.459 seconds for the small-file case.

Required measurement protocol:

1. Boot complete Compose stack on Pi 5 4 GB with MinIO test profile.
2. Record `docker stats --no-stream` after five idle minutes.
3. Archive one file larger than available RAM and record peak RSS.
4. Archive at least 10,000 small files at concurrency one and record peak RSS, CPU, elapsed time, and SQLite size.
5. Repeat with maximum concurrency two.
6. Exclude filesystem cache only when calculation and raw `/proc/meminfo` are recorded.

Target: idle stack below approximately 1.2 GB excluding filesystem cache.

## Full Compose stack

Raspberry Pi 5 idle measurement on 2026-07-28 after restart, with web, worker, SFTPGo, and MinIO:

- Mnema web: 94,352 KiB RSS.
- Mnema worker: 80,064 KiB RSS.
- SFTPGo: 57,328 KiB RSS.
- MinIO: 272,400 KiB RSS.
- Container process total: 504,144 KiB, approximately 492 MiB.
- Whole-host used memory: 776,704 KiB; available memory: 3,303,312 KiB.
- Swap used: 0.

This passes the approximately 1.2 GB idle target. Values came from container init process `/proc/<pid>/status`; `docker stats` returned unusable zero counters on this host.

## Disposable stress harness

Raspberry Pi 5 ARM64 measurements on 2026-07-28 used the temporary-data-only harness
with filesystem versioned backup, AES-256-GCM local cold storage, SQLite WAL, and
independent restores. They did not use Kopia or MinIO:

- 10,000 files of 4 KiB, concurrency one: 330.976 seconds, 109,488 KiB peak RSS,
  19,443,712-byte SQLite database, 120,000 audit events.
- 10,000 files of 4 KiB, concurrency two: 302.738 seconds, 108,944 KiB peak RSS,
  19,460,096-byte SQLite database, 120,000 audit events.
- One 5 GiB file on a host with 4,177,936,384 bytes physical memory: 774.774 seconds,
  71,952 KiB peak RSS, 45,056-byte SQLite database.

Every run produced matching item, snapshot, and encrypted-object counts. First/last
sample restores passed for both local and cold copies. Crash injection after download,
backup write, and encrypted upload also recovered without duplicate objects. Generated
data was automatically removed; the dedicated workspace returned to 4 KiB.

Still required: repeat larger-than-RAM and 10,000-file measurements through real
Kopia/MinIO adapters, capture reliable cgroup peaks while `docker stats` remains broken
on this host, and perform physical power-cut injection.

## Real Kopia and isolated MinIO

Raspberry Pi 5 ARM64 measurements on 2026-07-29 used Kopia 0.23.1 and a dedicated
MinIO `RELEASE.2025-07-23T15-54-02Z` container on an internal-only Docker network:

- One 1 GiB file, concurrency one: 271.125 seconds and 163,376 KiB peak RSS.
- 1,000 files of 4 KiB, concurrency one: 1,076.620 seconds and 101,616 KiB peak RSS.
- 1,000 files of 4 KiB, concurrency two: 573.858 seconds and 108,656 KiB peak RSS.

Both 1,000-file runs produced exactly 1,000 quarantined items, 12,000 audit events,
1,000 Kopia snapshots, and 1,000 encrypted MinIO objects. Independent restores of the
first and last items passed from both Kopia and MinIO. The 1 GiB run produced one
snapshot/object and passed both restores. Test containers mounted no production Mnema
database, configuration, secrets, active storage, Kopia repository, or MinIO data.
Temporary credentials, Docker networks, repositories, buckets, and generated data were
removed after each run.

Still required on the real adapters: 5 GiB larger-than-RAM scale, 10,000 files, MinIO
restart during active upload, missing-backup fault injection, and physical power loss.
