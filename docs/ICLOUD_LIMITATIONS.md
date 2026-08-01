# iCloud limitations and guarded cleanup

Mnema supports read-only iCloud Photos import through pinned `icloudpd`. Support is limited
to one account and Personal Library. Original photos, videos, RAW assets, and Live Photo
components are copied into active storage before Mnema independently verifies Kopia and
encrypted cold-storage restores.

Apple provides no supported public API for this appliance workflow. Authentication uses
iCloud web access, expires periodically, does not support hardware security keys, and
requires Advanced Data Protection to be disabled. Apple-side changes may break the client.

iCloud Drive, Shared Library, multi-account operation, and album filters are unsupported.
The local source adapter remains read-only and reports `can_delete=False`. Mnema never
supplies `icloudpd` deletion or local-sync flags because those cannot execute an immutable
approved manifest or confirm Apple-side absence.

Optional capacity relief is a separate control plane, disabled by default. It reads total
iCloud quota through Apple's private interface. At 90% usage it may propose oldest
non-favorite Personal Library assets whose complete component group has passed local,
Kopia, encrypted cold-restore, confirmed Glacier, and seven-day quarantine checks. One
manifest can contain at most 1,000 assets or 10% of account quota and targets 80% usage.

Execution requires the global deletion gate, open safety lock, fresh quota and inventory,
and explicit confirmation of the manifest digest. Each Apple ID and change tag is checked
again. Successful deletion means the exact asset is absent from Personal Library and
present in Recently Deleted. Mnema never empties Recently Deleted and never removes either
Pi copy. Ambiguous results stop the batch, close the global gate, and require manual review.

Apple provides no supported API or stability guarantee for quota and photo mutations.
Expired authentication, changed response shapes, rate limits, or missing facts therefore
fail closed. Production enablement requires the separately reviewed synthetic-account
destructive test described by project policy. Current build leaves
`MNEMA_ICLOUD_DELETION_MILESTONE_APPROVED=false`; appliance configuration cannot change it.
