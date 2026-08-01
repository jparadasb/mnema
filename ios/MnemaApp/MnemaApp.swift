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
            try await ensureFileProviderDomain()
            status = "Connected. Enable Mnema in Files."
            code = ""
        } catch {
            let failure = error as NSError
            status = "Connection failed: \(failure.localizedDescription) [\(failure.domain) \(failure.code)]"
        }
    }

    private func ensureFileProviderDomain() async throws {
        let identifier = NSFileProviderDomainIdentifier("mnema-owner")
        let domains: [NSFileProviderDomain] = try await withCheckedThrowingContinuation { continuation in
            NSFileProviderManager.getDomainsWithCompletionHandler { domains, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: domains)
                }
            }
        }
        guard !domains.contains(where: { $0.identifier == identifier }) else { return }
        let domain = NSFileProviderDomain(identifier: identifier, displayName: "Mnema")
        try await NSFileProviderManager.add(domain)
    }
}
