import Foundation
import Security
#if targetEnvironment(simulator)
import UIKit
#endif

enum TokenStore {
    private static let service = "com.jparadasb.mnema.fileprovider"
    #if targetEnvironment(simulator)
    private static let simulatorPasteboard = UIPasteboard.general
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
        var values = simulatorValues()
        values[account] = value
        simulatorPasteboard.string = try String(
            data: JSONSerialization.data(withJSONObject: values),
            encoding: .utf8
        )
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
        return simulatorValues()[account]
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

    #if targetEnvironment(simulator)
    private static func simulatorValues() -> [String: String] {
        guard let data = simulatorPasteboard.string?.data(using: .utf8),
              let values = try? JSONSerialization.jsonObject(with: data) as? [String: String]
        else { return [:] }
        return values
    }
    #endif
}
