import Combine
import FileProvider
import SwiftUI

@main
struct MnemaApp: App {
    var body: some Scene { WindowGroup { ConnectionView() } }
}

@MainActor
final class ConnectionModel: ObservableObject {
    enum State {
        case disconnected
        case connecting
        case connected
        case offline
        case error
    }

    @Published var state: State = .disconnected
    @Published var server = TokenStore.load(account: "serverURL") ?? ""
    @Published var code = ""
    @Published var deviceName = UIDevice.current.name
    @Published var statusMessage = "Pair this phone to access Mnema Files."
    @Published var pendingPairingDevice: String?
    @Published var lastSuccessfulSync = SharedSyncState.lastSuccessfulSync
    @Published var showDisconnectConfirmation = false

    private let domainIdentifier = NSFileProviderDomainIdentifier("mnema-owner")
    private let client = APIClient()

    var isConnected: Bool { state == .connected || state == .offline }
    var isBusy: Bool { state == .connecting }

    init() {
        Task { await restoreSession() }
    }

    func refreshDisplayState() {
        lastSuccessfulSync = SharedSyncState.lastSuccessfulSync
    }

    func restoreSession() async {
        refreshDisplayState()
        guard TokenStore.load(account: "refreshToken") != nil,
              TokenStore.load(account: "serverURL") != nil else {
            state = .disconnected
            return
        }
        state = .connecting
        statusMessage = "Checking your Mnema session…"
        do {
            let account = try await client.account()
            deviceName = account.deviceName
            try await ensureFileProviderDomain()
            state = .connected
            statusMessage = "Your archive is available in Files."
        } catch let error as APIError {
            if case .server(let code) = error, code == 401 {
                try? await removeFileProviderDomain()
                try? TokenStore.clearSession()
                state = .disconnected
                statusMessage = "Your session expired. Pair this phone again."
            } else {
                state = .error
                statusMessage = "Mnema could not validate this session."
            }
        } catch is URLError {
            state = .offline
            statusMessage = "Offline. Files will refresh when the server is reachable."
        } catch {
            state = .error
            statusMessage = "Mnema could not restore Files access."
        }
    }

    func connect() async {
        guard let url = URL(string: server), url.scheme == "https" else {
            state = .error
            statusMessage = "Enter a valid HTTPS server URL."
            return
        }
        guard !code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            state = .error
            statusMessage = "Enter the single-use pairing code."
            return
        }
        state = .connecting
        statusMessage = "Connecting this phone…"
        do {
            try await client.pair(server: url, code: code, deviceName: UIDevice.current.name)
            try await recreateFileProviderDomain()
            let account = try await client.account()
            deviceName = account.deviceName
            code = ""
            pendingPairingDevice = nil
            state = .connected
            statusMessage = "Connected. Your archive is available in Files."
        } catch {
            state = .error
            statusMessage = "Connection failed. Check the server and pairing code."
        }
    }

    func handlePairingURL(_ url: URL) {
        guard let pairing = PairingDeepLink.parse(url) else {
            statusMessage = "That Mnema pairing link is invalid or expired."
            state = .error
            return
        }
        server = pairing.server.absoluteString
        code = pairing.code
        pendingPairingDevice = pairing.deviceName
        state = .disconnected
        statusMessage = "Ready to connect this phone to \(pairing.server.host ?? "Mnema")."
    }

    func disconnect() async {
        state = .connecting
        statusMessage = "Disconnecting this phone…"
        do {
            try await removeFileProviderDomain()
            SharedSyncState.clearSyncAnchor()
            try TokenStore.clearSession()
            state = .disconnected
            statusMessage = "Disconnected locally. Other phones are unchanged."
        } catch {
            state = .error
            statusMessage = "Mnema could not disconnect this phone completely."
        }
    }

    func openFiles() async {
        do {
            guard let domain = try await domains().first(where: { $0.identifier == domainIdentifier }) else {
                try await ensureFileProviderDomain()
                guard let addedDomain = try await domains().first(where: { $0.identifier == domainIdentifier }) else {
                    throw CocoaError(.fileNoSuchFile)
                }
                try await openFiles(for: addedDomain)
                return
            }
            try await openFiles(for: domain)
        } catch {
            statusMessage = "Open Files and choose Mnema under Locations."
        }
    }

    private func openFiles(for domain: NSFileProviderDomain) async throws {
            guard let manager = NSFileProviderManager(for: domain) else {
                throw CocoaError(.fileNoSuchFile)
            }
            let url: URL = try await withCheckedThrowingContinuation { continuation in
                manager.getUserVisibleURL(for: .rootContainer) { url, error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else if let url {
                        continuation.resume(returning: url)
                    } else {
                        continuation.resume(throwing: CocoaError(.fileNoSuchFile))
                    }
                }
            }
            guard await UIApplication.shared.open(url) else {
                throw CocoaError(.fileNoSuchFile)
            }
        }

    private func domains() async throws -> [NSFileProviderDomain] {
        try await withCheckedThrowingContinuation { continuation in
            NSFileProviderManager.getDomainsWithCompletionHandler { domains, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: domains)
                }
            }
        }
    }

    private func ensureFileProviderDomain() async throws {
        if try await domains().contains(where: { $0.identifier == domainIdentifier }) { return }
        let domain = NSFileProviderDomain(identifier: domainIdentifier, displayName: "Mnema")
        try await NSFileProviderManager.add(domain)
    }

    private func recreateFileProviderDomain() async throws {
        try await removeFileProviderDomain()
        // Pairing anew means a different server generation; a carried-over
        // anchor would point past changes this device has never seen.
        SharedSyncState.clearSyncAnchor()
        let domain = NSFileProviderDomain(identifier: domainIdentifier, displayName: "Mnema")
        try await NSFileProviderManager.add(domain)
    }

    private func removeFileProviderDomain() async throws {
        if let existing = try await domains().first(where: { $0.identifier == domainIdentifier }) {
            try await NSFileProviderManager.remove(existing)
        }
    }
}

struct ConnectionView: View {
    @StateObject private var model = ConnectionModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 20) {
                    brandHeader
                    if model.isConnected { dashboard } else { pairingCard }
                    statusCard
                }
                .padding(20)
            }
            .background(Color(uiColor: .systemGroupedBackground))
            .navigationTitle("Mnema Files")
            .navigationBarTitleDisplayMode(.inline)
        }
        .onChange(of: scenePhase) { phase in
            if phase == .active { model.refreshDisplayState() }
        }
        .onOpenURL { url in model.handlePairingURL(url) }
        .confirmationDialog(
            "Disconnect this phone?",
            isPresented: $model.showDisconnectConfirmation,
            titleVisibility: .visible
        ) {
            Button("Disconnect", role: .destructive) { Task { await model.disconnect() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes Mnema from Files on this phone. Other phones and archived files are unchanged.")
        }
    }

    private var brandHeader: some View {
        VStack(spacing: 10) {
            Image("MnemaMark")
                .resizable()
                .scaledToFit()
                .frame(width: 88, height: 88)
                .accessibilityHidden(true)
            Text("Mnema")
                .font(.system(size: 34, weight: .bold, design: .rounded))
            Text("Your private, verified memory archive")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 12)
    }

    private var dashboard: some View {
        VStack(spacing: 0) {
            Toggle(isOn: Binding(
                get: { model.isConnected },
                set: { enabled in
                    if !enabled { model.showDisconnectConfirmation = true }
                }
            )) {
                Label(model.state == .offline ? "Connected · Offline" : "Connected", systemImage: "checkmark.shield.fill")
                    .foregroundStyle(model.state == .offline ? .orange : .cyan)
            }
            .disabled(model.isBusy)
            .accessibilityIdentifier("mnema.connected-toggle")
            Divider().padding(.vertical, 14)
            detailRow(title: "Server", value: URL(string: model.server)?.host ?? model.server)
            detailRow(title: "Device", value: model.deviceName)
            detailRow(title: "Last Files refresh", value: lastSyncText)
            Button { Task { await model.openFiles() } } label: {
                Label("Open Mnema in Files", systemImage: "folder.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Color(red: 0.0, green: 0.64, blue: 0.78))
            .padding(.top, 18)
            .accessibilityIdentifier("mnema.open-files")
        }
        .padding(18)
        .background(.background, in: RoundedRectangle(cornerRadius: 20))
    }

    private var pairingCard: some View {
        VStack(spacing: 16) {
            TextField("https://files.example.com", text: $model.server)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                .textContentType(.URL)
                .accessibilityIdentifier("mnema.server-url")
            SecureField("Pairing code", text: $model.code)
                .textContentType(.oneTimeCode)
                .accessibilityIdentifier("mnema.pairing-code")
            Button { Task { await model.connect() } } label: {
                if model.isBusy {
                    ProgressView().frame(maxWidth: .infinity)
                } else {
                    Text(model.pendingPairingDevice.map { "Connect \($0)" } ?? "Connect Mnema")
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(Color(red: 0.0, green: 0.64, blue: 0.78))
            .disabled(model.isBusy)
            .accessibilityIdentifier("mnema.connect")
        }
        .padding(18)
        .background(.background, in: RoundedRectangle(cornerRadius: 20))
    }

    private var statusCard: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: statusIcon)
                .foregroundStyle(statusColor)
            Text(model.statusMessage)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("mnema.connection-status")
            Spacer()
        }
        .padding(16)
        .background(.background, in: RoundedRectangle(cornerRadius: 16))
    }

    private func detailRow(title: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).foregroundStyle(.secondary)
            Spacer()
            Text(value).multilineTextAlignment(.trailing).lineLimit(2)
        }
        .font(.subheadline)
        .padding(.vertical, 5)
    }

    private var lastSyncText: String {
        guard let date = model.lastSuccessfulSync else { return "Waiting for Files" }
        return date.formatted(.relative(presentation: .named))
    }

    private var statusIcon: String {
        switch model.state {
        case .connected: return "checkmark.circle.fill"
        case .offline: return "wifi.slash"
        case .connecting: return "arrow.triangle.2.circlepath"
        case .error: return "exclamationmark.triangle.fill"
        case .disconnected: return "link.badge.plus"
        }
    }

    private var statusColor: Color {
        switch model.state {
        case .connected: return .green
        case .offline: return .orange
        case .error: return .red
        default: return .secondary
        }
    }
}
