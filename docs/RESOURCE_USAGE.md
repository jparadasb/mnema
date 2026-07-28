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

Still required: archive a file larger than RAM, run at least 10,000 small files, record peak whole-container cgroup memory during Kopia/MinIO activity, and repeat at concurrency two.
