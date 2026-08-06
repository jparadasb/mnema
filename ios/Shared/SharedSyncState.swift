import Foundation

enum SharedSyncState {
    private static var defaults: UserDefaults? {
        guard let identifier = Bundle.main.object(
            forInfoDictionaryKey: "AppGroupIdentifier"
        ) as? String else { return nil }
        return UserDefaults(suiteName: identifier)
    }

    private static let lastSuccessfulSyncKey = "mnema.lastSuccessfulFileProviderSync"

    static var lastSuccessfulSync: Date? {
        defaults?.object(forKey: lastSuccessfulSyncKey) as? Date
    }

    static func markSuccessfulSync(at date: Date = Date()) {
        defaults?.set(date, forKey: lastSuccessfulSyncKey)
    }
}
