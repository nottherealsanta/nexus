import Foundation
import SwiftUI
import NexusKit

struct AppFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

@MainActor final class AppStore: ObservableObject {
    @Published var editableURL = ""
    @Published private(set) var client: GatewayClient?
    @Published private(set) var selectedHost: String?
    @Published private(set) var selectedSession: String?
    @Published var draft = ""
    @Published var error: String?
    @Published private(set) var scopes: [String] = []
    @Published private(set) var hosts: [JSONValue] = []
    @Published private(set) var sessions: [JSONValue] = []
    @Published private(set) var snapshot: JSONValue?
    @Published private(set) var online = false
    @Published var showArchived = false
    @Published var allowLoopbackHTTP = false
    private var credential: PairingCredential?
    private var drafts: [String: String] = [:]

    init() {
        credential = Keychain.read()
        if let credential { editableURL = credential.boundURL; scopes = credential.scopes }
        drafts = UserDefaults.standard.dictionary(forKey: "nexus.drafts") as? [String: String] ?? [:]
        rebuildClient()
    }
    var isPaired: Bool { credential != nil }
    private func rebuildClient() {
        client = nil
        guard let url = URL(string: editableURL), let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let host = parts.host else { return }
        let bound = credential?.boundURL == url.absoluteString ? credential?.token : nil
        do { client = try GatewayClient(baseURL: url, token: bound, allowInsecureLoopback: allowLoopbackHTTP) }
        catch { self.error = error.localizedDescription }
    }
    func pair(code: String) async {
        do {
            guard let url = URL(string: editableURL) else { throw AppFailure("Enter a valid gateway URL") }
            let pairingClient = try GatewayClient(baseURL: url, allowInsecureLoopback: allowLoopbackHTTP)
            let response = try await pairingClient.pair(code: code, deviceName: "Nexus iOS")
            let saved = PairingCredential(token: response.token, scopes: response.scopes, deviceID: response.device_id, boundURL: url.absoluteString)
            try Keychain.save(saved); credential = saved; scopes = saved.scopes; editableURL = saved.boundURL; rebuildClient(); await refresh()
        } catch { self.error = error.localizedDescription }
    }
    func forget() {
        Keychain.remove(); credential = nil; scopes = []; client = nil; hosts = []; sessions = []; snapshot = nil
        selectedHost = nil; selectedSession = nil; drafts.removeAll(); UserDefaults.standard.removeObject(forKey: "nexus.drafts"); draft = ""; online = false
    }
    func refresh() async {
        rebuildClient(); guard let client else { online = false; return }
        do { _ = try await client.health(); let result = try await client.hosts(); hosts = result.map { .object(["host_id": .string($0.host_id), "workspace": $0.workspace.map(JSONValue.string) ?? .null, "label": $0.label.map(JSONValue.string) ?? .null, "state": $0.state.map(JSONValue.string) ?? .null]) }; online = true; error = nil }
        catch { online = false; handle(error) }
    }
    func selectHost(_ id: String?) async {
        selectedHost = id; selectedSession = nil; sessions = []; snapshot = nil; draft = ""
        guard let id, let client else { return }
        do { let result = try await client.sessions(hostID: id); sessions = Self.rows(result); }
        catch { handle(error) }
    }
    func selectSession(_ id: String?) async {
        if let old = selectedSession { drafts[draftKey(old)] = draft }
        selectedSession = id; snapshot = nil
        guard let id, let selectedHost, let client else { draft = ""; return }
        draft = drafts[draftKey(id)] ?? ""
        do { snapshot = try await client.snapshot(hostID: selectedHost, sessionID: id) }
        catch { handle(error) }
    }
    private func draftKey(_ session: String) -> String { "\(selectedHost ?? ""):" + session }
    func updateDraft(_ value: String) { draft = value; if let selectedSession { drafts[draftKey(selectedSession)] = value; UserDefaults.standard.set(drafts, forKey: "nexus.drafts") } }
    func commandPayload(_ payload: [String: JSONValue]) async throws -> JSONValue {
        guard let client, let selectedHost else { throw AppFailure("Select a connected host") }
        do { let result = try await client.command(hostID: selectedHost, payload: .object(payload)); if let selectedSession { snapshot = try await client.snapshot(hostID: selectedHost, sessionID: selectedSession) }; return result }
        catch { handle(error); throw error }
    }
    func send() async {
        let sent = draft
        do { _ = try await commandPayload(["type": .string("session.enqueue"), "session_id": .string(selectedSession ?? ""), "text": .string(sent)])
            if draft == sent { updateDraft("") }
        } catch { }
    }
    func reload() async { await refresh(); if let selectedHost { await selectHost(selectedHost) }; if let selectedSession { await selectSession(selectedSession) } }
    private func handle(_ error: Error) {
        self.error = error.localizedDescription
        if case NexusError.httpStatus(let code, _) = error, code == 401 { Keychain.remove(); credential = nil; scopes = []; client = nil }
    }
    private static func rows(_ value: JSONValue) -> [JSONValue] { if case .array(let rows) = value { return rows }; if case .object(let object) = value, case .array(let rows)? = object["sessions"] { return rows }; return [] }
}

struct RootView: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        NavigationSplitView {
            List(selection: Binding(get: { store.selectedSession }, set: { id in Task { await store.selectSession(id) } })) {
                Section("Gateway") {
                    TextField("https://gateway.example", text: $store.editableURL).textInputAutocapitalization(.never).keyboardType(.URL)
                    #if DEBUG
                    Toggle("Allow loopback HTTP", isOn: $store.allowLoopbackHTTP).onChange(of: store.allowLoopbackHTTP) { _, _ in Task { await store.refresh() } }
                    #endif
                    HStack { Button("Connect") { Task { await store.refresh() } }; if store.isPaired { Button("Forget", role: .destructive) { store.forget() } } }
                    Text(store.online ? "Connected" : "Offline or stale").font(.caption)
                    if let error = store.error { Text(error).foregroundStyle(.red).font(.caption) }
                }
                Section("Hosts") { ForEach(store.hosts.indices, id: \.self) { index in
                    let host = store.hosts[index]; let id = host["host_id"]?.string ?? ""
                    Button(host["label"]?.string ?? id) { Task { await store.selectHost(id) } }
                } }
                Section("Sessions") { ForEach(store.sessions.indices, id: \.self) { index in
                    let row = store.sessions[index]; let id = row["id"]?.string ?? row["session_id"]?.string ?? ""
                    Text(row["title"]?.string ?? id).tag(id)
                } }
            }.navigationTitle("Nexus")
        } detail: { ConversationView() }
        .toolbar { ToolbarItemGroup(placement: .primaryAction) {
            Button("Dictation", systemImage: "mic") {}.accessibilityLabel("Dictation")
            Button("Live call", systemImage: "phone") {}.accessibilityLabel("Live call")
        } }
    }
}

private extension JSONValue {
    subscript(_ key: String) -> JSONValue? { if case .object(let values) = self { return values[key] }; return nil }
}
