import XCTest
@testable import Mnema

final class ModelsTests: XCTestCase {
    func testTokenPayloadDecodes() throws {
        let data = #"{"accessToken":"a","refreshToken":"r","expiresIn":900,"deviceId":"d"}"#.data(using: .utf8)!
        let tokens = try JSONDecoder().decode(APITokens.self, from: data)
        XCTAssertEqual(tokens.expiresIn, 900)
    }
}
