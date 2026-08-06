import Foundation

struct PairingDeepLink: Equatable {
    let server: URL
    let code: String
    let deviceName: String?

    static func parse(_ url: URL) -> PairingDeepLink? {
        guard url.scheme == "mnema", url.host == "pair" else { return nil }
        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        guard
            let serverValue = components?.queryItems?.first(where: { $0.name == "server" })?.value,
            let server = URL(string: serverValue),
            server.scheme == "https",
            server.host != nil,
            server.user == nil,
            server.password == nil,
            let code = components?.queryItems?.first(where: { $0.name == "code" })?.value,
            !code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return nil }
        let deviceName = components?.queryItems?.first(where: { $0.name == "device" })?.value
        return PairingDeepLink(server: server, code: code, deviceName: deviceName)
    }
}

struct APITokens: Codable {
    let accessToken: String
    let refreshToken: String
    let expiresIn: Int
    let deviceId: String
}

struct AccountStatus: Codable {
    let rootItemId: String
    let generation: String
    let deviceId: String
    let deviceName: String
}

struct RemoteItem: Codable, Identifiable {
    let id: String
    let parentId: String?
    let name: String
    let kind: String
    let size: Int64
    let contentType: String
    let contentVersion: String
    let metadataVersion: String
    let modifiedAt: String
    let status: String
    let error: String?
    let capabilities: [String]
}

struct ChildrenPage: Codable {
    let items: [RemoteItem]
    let nextOffset: Int?
}

struct RemoteChange: Codable {
    let operation: String
    let itemId: String
    let item: RemoteItem?
}

struct ChangesPage: Codable {
    let changes: [RemoteChange]
    let nextCursor: String
    let resetRequired: Bool
    let more: Bool
}

struct UploadStart: Codable {
    let uploadId: String
    let itemId: String
    let offset: Int64
    let expiresAt: String
}
