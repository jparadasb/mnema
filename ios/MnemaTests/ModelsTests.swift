import XCTest
@testable import Mnema

final class ModelsTests: XCTestCase {
    func testTokenPayloadDecodes() throws {
        let data = #"{"accessToken":"a","refreshToken":"r","expiresIn":900,"deviceId":"d"}"#.data(using: .utf8)!
        let tokens = try JSONDecoder().decode(APITokens.self, from: data)
        XCTAssertEqual(tokens.expiresIn, 900)
    }

    func testAccountStatusDecodesDeviceMetadata() throws {
        let data = #"{"rootItemId":"root","generation":"g","deviceId":"d","deviceName":"Hydra"}"#.data(using: .utf8)!
        let account = try JSONDecoder().decode(AccountStatus.self, from: data)
        XCTAssertEqual(account.deviceId, "d")
        XCTAssertEqual(account.deviceName, "Hydra")
    }

    func testPairingDeepLinkParsesHTTPSServerAndCode() {
        let url = URL(string: "mnema://pair?server=https%3A%2F%2Ffiles.example.com&code=abc123&device=Hydra")!
        let link = PairingDeepLink.parse(url)
        XCTAssertEqual(link?.server.absoluteString, "https://files.example.com")
        XCTAssertEqual(link?.code, "abc123")
        XCTAssertEqual(link?.deviceName, "Hydra")
    }

    func testPairingDeepLinkRejectsUnsafeServer() {
        let url = URL(string: "mnema://pair?server=http%3A%2F%2Fevil.example&code=abc123")!
        XCTAssertNil(PairingDeepLink.parse(url))
    }
}
