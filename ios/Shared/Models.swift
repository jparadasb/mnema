import Foundation

struct APITokens: Codable {
    let accessToken: String
    let refreshToken: String
    let expiresIn: Int
    let deviceId: String
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
    let modifiedAt: Date
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
    let expiresAt: Date
}
