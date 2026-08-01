import FileProvider
import SwiftUI

@main
struct MnemaApp: App {
    var body: some Scene { WindowGroup { PairingView() } }
}

struct PairingView: View {
    @State private var server = ""
    @State private var code = ""
    @State private var status = "Not connected"

    var body: some View {
        NavigationStack {
            Form {
                TextField("https://files.example.com", text: $server)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                SecureField("Pairing code", text: $code)
                Button("Connect Mnema") {
                    Task { await connect() }
                }
                Text(status).foregroundStyle(.secondary)
            }
            .navigationTitle("Mnema Files")
        }
    }

    @MainActor
    private func connect() async {
        guard let url = URL(string: server), url.scheme == "https" else {
            status = "Enter a valid HTTPS URL"
            return
        }
        do {
            try await APIClient().pair(server: url, code: code, deviceName: UIDevice.current.name)
            let domain = NSFileProviderDomain(
                identifier: NSFileProviderDomainIdentifier("mnema-owner"),
                displayName: "Mnema"
            )
            try await NSFileProviderManager.add(domain)
            status = "Connected. Enable Mnema in Files."
            code = ""
        } catch {
            status = "Connection failed: \(error.localizedDescription)"
        }
    }
}
