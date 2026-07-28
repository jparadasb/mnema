# iCloud limitations

No iCloud authentication, discovery, download, photos access, or deletion exists in this milestone. Placeholder adapters throw `NotImplementedError`.

iCloud Drive and iCloud Photos must be treated as distinct providers. Stable identity, version semantics, metadata fidelity, authentication expiry, rate limits, optimized/original assets, shared content, deletability, and confirmed absence must be researched independently before implementation.

No future adapter may enable deletion until its capabilities and ambiguous-result recovery are proven with synthetic accounts.

