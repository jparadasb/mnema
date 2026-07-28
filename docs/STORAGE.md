# Storage

Active and backup paths must be different filesystems and, before production deletion, different physical devices. Mnema records filesystem UUID when host integration exposes it and compares mounted `st_dev` at runtime.

`st_dev` alone cannot prove physical independence through every USB bridge, device mapper, bind mount, or network filesystem. Hardware validation must trace parent block devices by `/sys` and `lsblk` before production deletion.

Staging should reside on active filesystem so atomic rename is guaranteed. Current configuration permits a separate staging path; deployment must verify same filesystem before production use.

Never mount backup repository into SFTPGo. Do not configure RAID by default.

