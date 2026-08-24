import Foundation

enum SharedSyncState {
    private static var defaults: UserDefaults? {
        guard let identifier = Bundle.main.object(
            forInfoDictionaryKey: "AppGroupIdentifier"
        ) as? String else { return nil }
        return UserDefaults(suiteName: identifier)
    }

    private static let lastSuccessfulSyncKey = "mnema.lastSuccessfulFileProviderSync"
    private static let syncAnchorKey = "mnema.fileProviderSyncAnchor"

    static var lastSuccessfulSync: Date? {
        defaults?.object(forKey: lastSuccessfulSyncKey) as? Date
    }

    static func markSuccessfulSync(at date: Date = Date()) {
        defaults?.set(date, forKey: lastSuccessfulSyncKey)
    }

    /// The change cursor the extension has actually reported items up to.
    ///
    /// This must never be derived from the server's newest cursor: answering
    /// `currentSyncAnchor` with "the latest thing that exists" tells the system
    /// it is already up to date, so every change in between is never requested.
    static var syncAnchor: String? {
        defaults?.string(forKey: syncAnchorKey)
    }

    static func storeSyncAnchor(_ cursor: String) {
        defaults?.set(cursor, forKey: syncAnchorKey)
    }

    static func clearSyncAnchor() {
        defaults?.removeObject(forKey: syncAnchorKey)
    }
}
