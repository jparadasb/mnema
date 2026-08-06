import CryptoKit
import FileProvider
import Foundation

enum APIError: Error { case notConfigured, invalidResponse, server(Int) }

final class APIClient {
    private let decoder: JSONDecoder = {
        let value = JSONDecoder()
        value.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let encoded = try container.decode(String.self)
            var normalized = encoded
            if let decimal = encoded.firstIndex(of: ".") {
                let fractionStart = encoded.index(after: decimal)
                let timezoneStart = encoded[fractionStart...].firstIndex { !$0.isNumber }
                    ?? encoded.endIndex
                let fraction = encoded[fractionStart..<timezoneStart]
                if !fraction.isEmpty {
                    let milliseconds = String(fraction.prefix(3))
                        .padding(toLength: 3, withPad: "0", startingAt: 0)
                    normalized = String(encoded[...decimal])
                        + milliseconds
                        + String(encoded[timezoneStart...])
                }
            }
            let fractional = ISO8601DateFormatter()
            fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = fractional.date(from: normalized) { return date }
            let standard = ISO8601DateFormatter()
            standard.formatOptions = [.withInternetDateTime]
            if let date = standard.date(from: encoded) { return date }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Expected an ISO 8601 date"
            )
        }
        return value
    }()
    private let encoder = JSONEncoder()
    private let session: URLSession

    init(session: URLSession = .shared) { self.session = session }

    private var baseURL: URL? { TokenStore.load(account: "serverURL").flatMap(URL.init(string:)) }

    private func request(
        _ path: String,
        method: String = "GET",
        body: Data? = nil,
        queryItems: [URLQueryItem] = []
    ) throws -> URLRequest {
        guard let pathURL = baseURL?.appending(path: path) else { throw APIError.notConfigured }
        var components = URLComponents(url: pathURL, resolvingAgainstBaseURL: false)
        if !queryItems.isEmpty { components?.queryItems = queryItems }
        guard let url = components?.url else { throw APIError.invalidResponse }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token = TokenStore.load(account: "accessToken") {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func response(for request: URLRequest, retryingAuthentication: Bool = true) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        if http.statusCode == 401, retryingAuthentication, TokenStore.load(account: "refreshToken") != nil {
            try await refreshAccessToken()
            var retried = request
            guard let token = TokenStore.load(account: "accessToken") else { throw APIError.server(401) }
            retried.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            return try await self.response(for: retried, retryingAuthentication: false)
        }
        return (data, http)
    }

    private func data<T: Decodable>(for request: URLRequest, as type: T.Type) async throws -> T {
        let (data, http) = try await response(for: request)
        if http.statusCode == 404 { throw NSFileProviderError(.noSuchItem) }
        guard (200..<300).contains(http.statusCode) else { throw APIError.server(http.statusCode) }
        return try decoder.decode(type, from: data)
    }

    private func refreshAccessToken() async throws {
        guard let refreshToken = TokenStore.load(account: "refreshToken") else { throw APIError.notConfigured }
        let body = try encoder.encode(["refresh_token": refreshToken])
        let refreshRequest = try request("v1/auth/refresh", method: "POST", body: body)
        let (data, http) = try await response(for: refreshRequest, retryingAuthentication: false)
        guard (200..<300).contains(http.statusCode) else { throw APIError.server(http.statusCode) }
        let tokens = try decoder.decode(APITokens.self, from: data)
        try TokenStore.save(tokens.accessToken, account: "accessToken")
        try TokenStore.save(tokens.refreshToken, account: "refreshToken")
    }

    func pair(server: URL, code: String, deviceName: String) async throws {
        let body = try encoder.encode(["code": code, "device_name": deviceName])
        var pairRequest = URLRequest(url: server.appending(path: "v1/auth/pair"))
        pairRequest.httpMethod = "POST"
        pairRequest.httpBody = body
        pairRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: pairRequest)
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else { throw APIError.server(http.statusCode) }
        let tokens = try decoder.decode(APITokens.self, from: data)
        try TokenStore.save(server.absoluteString, account: "serverURL")
        try TokenStore.save(tokens.accessToken, account: "accessToken")
        try TokenStore.save(tokens.refreshToken, account: "refreshToken")
    }

    func account() async throws -> AccountStatus {
        try await data(for: request("v1/account"), as: AccountStatus.self)
    }

    func item(_ id: String) async throws -> RemoteItem {
        try await data(for: request("v1/items/\(id)"), as: RemoteItem.self)
    }

    func children(_ id: String, offset: Int = 0) async throws -> ChildrenPage {
        try await data(
            for: request(
                "v1/items/\(id)/children",
                queryItems: [URLQueryItem(name: "offset", value: String(offset))]
            ),
            as: ChildrenPage.self
        )
    }

    func changes(cursor: String?) async throws -> ChangesPage {
        let queryItems = cursor.map { [URLQueryItem(name: "cursor", value: $0)] } ?? []
        return try await data(
            for: request("v1/changes", queryItems: queryItems),
            as: ChangesPage.self
        )
    }

    func download(_ id: String) async throws -> URL {
        var contentRequest = try request("v1/items/\(id)/content")
        var result = try await session.download(for: contentRequest)
        if let http = result.1 as? HTTPURLResponse, http.statusCode == 401 {
            try await refreshAccessToken()
            guard let token = TokenStore.load(account: "accessToken") else { throw APIError.server(401) }
            contentRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            result = try await session.download(for: contentRequest)
        }
        guard let http = result.1 as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIError.invalidResponse
        }
        return result.0
    }

    func delete(_ id: String) async throws {
        let (data, http) = try await response(for: request("v1/items/\(id)", method: "DELETE"))
        _ = data
        if http.statusCode == 404 { return }
        guard http.statusCode == 204 else { throw APIError.server(http.statusCode) }
    }

    func upload(file: URL, name: String, contentType: String) async throws -> String {
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        let size = (attributes[.size] as? NSNumber)?.int64Value ?? 0
        let digest = try sha256(file)
        let body = try encoder.encode(
            UploadMetadata(
                name: name,
                size: size,
                contentType: contentType,
                sha256: digest
            )
        )
        let started: UploadStart = try await data(for: request("v1/uploads", method: "POST", body: body), as: UploadStart.self)
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var offset: Int64 = 0
        while let chunk = try handle.read(upToCount: 8 * 1024 * 1024), !chunk.isEmpty {
            var chunkRequest = try request("v1/uploads/\(started.uploadId)", method: "PATCH")
            chunkRequest.httpBody = chunk
            chunkRequest.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            chunkRequest.setValue(String(offset), forHTTPHeaderField: "Upload-Offset")
            let _: UploadOffset = try await data(for: chunkRequest, as: UploadOffset.self)
            offset += Int64(chunk.count)
        }
        let result: UploadComplete = try await data(
            for: request("v1/uploads/\(started.uploadId)/complete", method: "POST"),
            as: UploadComplete.self
        )
        return result.itemId
    }

    private func sha256(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var digest = SHA256()
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty { digest.update(data: chunk) }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

private struct UploadOffset: Codable { let offset: Int64 }
private struct UploadComplete: Codable { let itemId: String; let status: String }
private struct UploadMetadata: Encodable {
    let name: String
    let size: Int64
    let contentType: String
    let sha256: String

    enum CodingKeys: String, CodingKey {
        case name, size, sha256
        case contentType = "content_type"
    }
}
