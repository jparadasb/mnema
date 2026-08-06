import SwiftUI

@main
struct MnemaE2EFixtureApp: App {
    var body: some Scene {
        WindowGroup {
            Text("Mnema E2E Fixture")
                .accessibilityIdentifier("mnema.e2e-fixture.ready")
        }
    }
}
