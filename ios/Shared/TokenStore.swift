import Foundation
import Security

enum TokenStore {
    private static let service = "com.jparadasb.mnema.fileprovider"
    #if targetEnvironment(simulator)
    private static var simulatorDefaults: UserDefaults? {
        guard let identifier = Bundle.main.object(
            forInfoDictionaryKey: "AppGroupIdentifier"
        ) as? String else { return nil }
        return UserDefaults(suiteName: identifier)
    }
    #endif
    private static var group: String? {
        Bundle.main.object(forInfoDictionaryKey: "KeychainAccessGroup") as? String
    }

    private static func baseQuery(account: String) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let group { query[kSecAttrAccessGroup as String] = group }
        return query
    }

    static func save(_ value: String, account: String) throws {
        #if targetEnvironment(simulator)
        guard let defaults = simulatorDefaults else { throw CocoaError(.fileNoSuchFile) }
        defaults.set(value, forKey: "\(service).\(account)")
        guard defaults.synchronize() else { throw CocoaError(.fileWriteUnknown) }
        return
        #else
        let data = Data(value.utf8)
        let query = baseQuery(account: account)
        SecItemDelete(query as CFDictionary)
        var insert = query
        insert[kSecValueData as String] = data
        insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(insert as CFDictionary, nil)
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        #endif
    }

    static func load(account: String) -> String? {
        #if targetEnvironment(simulator)
        return simulatorDefaults?.string(forKey: "\(service).\(account)")
        #else
        var query = baseQuery(account: account)
        query.merge([
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]) { _, new in new }
        var value: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &value) == errSecSuccess,
              let data = value as? Data else { return nil }
        return String(data: data, encoding: .utf8)
        #endif
    }

    static func remove(account: String) throws {
        #if targetEnvironment(simulator)
        guard let defaults = simulatorDefaults else { throw CocoaError(.fileNoSuchFile) }
        defaults.removeObject(forKey: "\(service).\(account)")
        guard defaults.synchronize() else { throw CocoaError(.fileWriteUnknown) }
        #else
        let status = SecItemDelete(baseQuery(account: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
        #endif
    }

    static func clearSession() throws {
        try remove(account: "accessToken")
        try remove(account: "refreshToken")
    }
}
