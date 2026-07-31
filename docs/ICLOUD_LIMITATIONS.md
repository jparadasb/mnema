# iCloud limitations

Mnema supports read-only iCloud Photos import through pinned `icloudpd`. Support is limited
to one account and Personal Library. Original photos, videos, RAW assets, and Live Photo
components are copied into active storage before Mnema independently verifies Kopia and
encrypted cold-storage restores.

Apple provides no supported public API for this appliance workflow. Authentication uses
iCloud web access, expires periodically, does not support hardware security keys, and
requires Advanced Data Protection to be disabled. Apple-side changes may break the client.

iCloud Drive, Shared Library, multi-account operation, album filters, and Apple-source
deletion are unsupported. Mnema never supplies `icloudpd` deletion or local-sync deletion
flags. The source adapter reports `can_delete=False` and rejects every delete call.

No future adapter may enable deletion until a separate reviewed milestone proves stable
identity, version semantics, ambiguous-result recovery, and confirmed absence using
synthetic accounts.
