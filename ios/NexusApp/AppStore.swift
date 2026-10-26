import Foundation
import SwiftUI
import NexusKit

struct AppFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Owns one gateway connection and one selected session. Epochs reject late responses;
/// snapshot-then-subscribe closes the event race using the host's durable cursor.
@MainActor final class AppStore: ObservableObject {
    @Published var editableURL = ""
    @Published private(set) var client: GatewayClient?
    @Published private(set) var selectedHost: String?
    @Published private(set) var selectedSession: String?
    @Published private(set) var draft = ""
    @Published var error: String?
    @Published private(set) var scopes: [String] = []
    @Published private(set) var hosts: [JSONValue] = []
    @Published private(set) var sessions: [JSONValue] = []
    @Published private(set) var snapshot: JSONValue?
    @Published private(set) var online = false
    @Published private(set) var busy = false
    @Published private(set) var loading = false
    @Published private(set) var loadingSessions = false
    @Published private(set) var connecting = false
    @Published private(set) var streaming = false
    @Published private(set) var truncated = false
    @Published private(set) var lastUpdated: Date?
    private var credential: PairingCredential?
    private var drafts: [String: String]
    private let defaults: UserDefaults
    private let transport: URLSession
    private let saveCredential: (PairingCredential) throws -> Void
    private let removeCredential: () -> Void
    private var connectionEpoch = UUID()
    private var selectionEpoch = UUID()
    private var hostEpoch = UUID()
    private var snapshotRequest = UUID()
    private var projection = ViewStore()
    private var streamTask: Task<Void, Never>?
    private var foreground = true

    init(defaults: UserDefaults = .standard, credential: PairingCredential? = Keychain.read(),
         transport: URLSession = .shared,
         saveCredential: @escaping (PairingCredential) throws -> Void = Keychain.save,
         removeCredential: @escaping () -> Void = Keychain.remove) {
        self.defaults = defaults; self.credential = credential; self.transport = transport
        self.saveCredential = saveCredential; self.removeCredential = removeCredential
        drafts = defaults.dictionary(forKey: "nexus.drafts") as? [String: String] ?? [:]
        if let credential { editableURL = credential.boundURL; scopes = credential.scopes }
        rebuildClient()
    }
    var isPaired: Bool { credential != nil }
    var canPrompt: Bool { online && scopes.contains("prompt") }
    var conversation: JSONValue? { snapshot?["view"] }
    var sessionTitle: String {
        let title = sessions.first { $0["id"]?.string == selectedSession }?["title"]?.string ?? ""
        return title.isEmpty ? "New conversation" : title
    }
    var pendingPermissions: [JSONValue] {
        (conversation?["permissions"]?.array ?? []).filter { $0["status"]?.string == "pending" }
    }
    private func rebuildClient() {
        client = nil
        guard let credential, let url = URL(string: credential.boundURL) else { return }
        client = try? GatewayClient(baseURL: url, token: credential.token, session: transport)
    }
    func pair(code: String) async {
        guard !connecting else { return }
        let epoch = connectionEpoch
        connecting = true; error = nil
        defer { if connectionEpoch == epoch { connecting = false } }
        do {
            let text = editableURL.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let url = URL(string: text) else { throw NexusError.invalidURL }
            let pairingClient = try GatewayClient(baseURL: url, session: transport)
            let response = try await pairingClient.pair(code: code.trimmingCharacters(in: .whitespacesAndNewlines), deviceName: "Nexus iOS")
            guard connectionEpoch == epoch else { return }
            let saved = PairingCredential(token: response.token, scopes: response.scopes, deviceID: response.device_id, boundURL: url.absoluteString)
            try saveCredential(saved)
            credential = saved; scopes = saved.scopes; editableURL = saved.boundURL; rebuildClient()
            connecting = false
            await reload()
        } catch { if connectionEpoch == epoch { handle(error) } }
    }
    func forget() {
        stopStream(); connectionEpoch = UUID(); hostEpoch = UUID(); selectionEpoch = UUID()
        removeCredential(); credential = nil; scopes = []; client = nil; hosts = []; sessions = []; snapshot = nil
        selectedHost = nil; selectedSession = nil; drafts.removeAll(); defaults.removeObject(forKey: "nexus.drafts")
        draft = ""; online = false; busy = false; loading = false; loadingSessions = false
        connecting = false; error = nil; truncated = false; lastUpdated = nil; loadingSessions = false; projection = ViewStore()
    }
    func refresh() async {
        guard let client else { return }
        let epoch = connectionEpoch
        connecting = true
        defer { if connectionEpoch == epoch { connecting = false } }
        do {
            let result = try await client.hosts()
            guard connectionEpoch == epoch else { return }
            hosts = result.map { .object(["host_id": .string($0.host_id), "workspace": $0.workspace.map(JSONValue.string) ?? .null, "label": $0.label.map(JSONValue.string) ?? .null, "state": $0.state.map(JSONValue.string) ?? .null]) }
            online = true; error = nil
            if let selectedHost, !result.contains(where: { $0.host_id == selectedHost }) { await selectHost(nil) }
            if selectedHost == nil, result.count == 1 { await selectHost(result[0].host_id) }
        } catch { if connectionEpoch == epoch { online = false; handle(error) } }
    }
    func selectHost(_ id: String?) async {
        updateDraft(draft); stopStream(); selectionEpoch = UUID(); hostEpoch = UUID()
        selectedHost = id?.isEmpty == false ? id : nil
        selectedSession = nil; sessions = []; snapshot = nil; draft = ""; loading = false; busy = false
        truncated = false; lastUpdated = nil; loadingSessions = false; projection = ViewStore()
        await refreshSessions()
    }
    func refreshSessions() async {
        guard let id = selectedHost, let client else { return }
        let epoch = hostEpoch
        loadingSessions = true
        defer { if epoch == hostEpoch { loadingSessions = false } }
        do {
            let result = try await client.sessions(hostID: id)
            guard epoch == hostEpoch else { return }
            sessions = result.array ?? result["sessions"]?.array ?? []
            truncated = result["truncated"]?.bool == true
            online = true; error = nil
        } catch { if epoch == hostEpoch { online = false; handle(error) } }
    }
    func selectSession(_ id: String?) async {
        updateDraft(draft); stopStream(); selectionEpoch = UUID()
        selectedSession = id; snapshot = nil; projection = ViewStore(); busy = false; lastUpdated = nil
        draft = id.map { drafts[draftKey($0)] ?? "" } ?? ""
        guard id != nil else { loading = false; return }
        await refreshConversation()
    }
    private func draftKey(_ session: String) -> String { "\(credential?.boundURL ?? ""):\(selectedHost ?? ""):\(session)" }
    func updateDraft(_ value: String) {
        draft = value
        guard let selectedSession else { return }
        let key = draftKey(selectedSession)
        if value.isEmpty { drafts.removeValue(forKey: key) } else { drafts[key] = value }
        defaults.set(drafts, forKey: "nexus.drafts")
    }
    func createSession() async {
        guard !busy, canPrompt, let client, let host = selectedHost else { return }
        let epoch = hostEpoch
        busy = true; error = nil
        defer { if epoch == hostEpoch { busy = false } }
        let id = UUID().uuidString.lowercased()
        do {
            let result = try await client.createSession(hostID: host, sessionID: id)
            guard epoch == hostEpoch else { return }
            guard result["session"]?["id"]?.string == id else { throw AppFailure(result["message"]?.string ?? "Session creation was not confirmed. Refresh the session list before trying again.") }
            await refreshSessions()
            guard epoch == hostEpoch else { return }
            await selectSession(id)
        } catch { if epoch == hostEpoch { handle(error, write: true) } }
    }
    func send() async {
        guard !busy, canPrompt, let session = selectedSession, let host = selectedHost, let client,
              !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let sent = draft; let key = draftKey(session); let epoch = selectionEpoch; let connection = connectionEpoch
        busy = true; error = nil
        defer { if epoch == selectionEpoch { busy = false } }
        do {
            let result = try await client.enqueue(hostID: host, sessionID: session, content: sent, requestID: UUID())
            guard connection == connectionEpoch else { return }
            guard result["type"]?.string == "SessionEnqueueResult" else { throw AppFailure(result["message"]?.string ?? "Message acceptance was not confirmed") }
            // An accepted message must not leave a duplicate draft when the user navigates away.
            if drafts[key] == sent { drafts.removeValue(forKey: key); defaults.set(drafts, forKey: "nexus.drafts") }
            guard epoch == selectionEpoch else { return }
            if draft == sent { updateDraft("") }
            await refreshConversation()
        } catch { if epoch == selectionEpoch { handle(error, write: true) } }
    }
    func stopTurn() async {
        guard canPrompt else { return }
        await perform(["type": .string("SessionCancel"), "drop_queue": .bool(false), "reason": .string("Stopped from iOS")], expected: "SessionCancelResult")
    }
    func resolvePermission(_ id: String, decision: String) async {
        guard scopes.contains("approve"), pendingPermissions.contains(where: { $0["id"]?.string == id }) else { return }
        await perform(["type": .string("PermissionResolve"), "request_id": .string(id), "decision": .string(decision)], expected: "PermissionResolveResult")
    }
    private func perform(_ values: [String: JSONValue], expected: String) async {
        guard !busy, online, let client, let host = selectedHost, let session = selectedSession else { return }
        let epoch = selectionEpoch
        busy = true; error = nil
        defer { if epoch == selectionEpoch { busy = false } }
        do {
            var payload = values; payload["session"] = .string(session)
            let result = try await client.command(hostID: host, payload: .object(payload))
            guard epoch == selectionEpoch else { return }
            guard result["type"]?.string == expected else { throw AppFailure(result["message"]?.string ?? "The action was not confirmed") }
            let alreadyAnswered = expected == "PermissionResolveResult" && result["resolved"]?.bool != true
            await refreshConversation()
            if epoch == selectionEpoch && alreadyAnswered { error = "This request was already answered or is no longer available." }
        } catch { if epoch == selectionEpoch { handle(error, write: true) } }
    }
    func refreshConversation() async {
        guard let client, let host = selectedHost, let session = selectedSession else { return }
        let epoch = selectionEpoch
        let request = UUID(); snapshotRequest = request
        stopStream(); loading = snapshot == nil
        defer { if epoch == selectionEpoch && request == snapshotRequest { loading = false } }
        do {
            let value = try await client.snapshot(hostID: host, sessionID: session)
            guard epoch == selectionEpoch && request == snapshotRequest else { return }
            var candidate = ViewStore()
            guard candidate.apply(snapshot: value) == .applied, candidate.sessionID == session else { throw NexusError.invalidResponse("The session snapshot does not match this conversation") }
            projection = candidate; snapshot = value; online = true; error = nil; lastUpdated = Date()
            startStream()
        } catch { if epoch == selectionEpoch && request == snapshotRequest { online = false; handle(error); scheduleReconnect() } }
    }
    func reload() async {
        await refresh()
        await refreshSessions()
        await refreshConversation()
    }
    func setForeground(_ active: Bool) async {
        foreground = active
        if active { await reload() } else { stopStream() }
    }
    private func stopStream() { streamTask?.cancel(); streamTask = nil; streaming = false }
    private func startStream() {
        guard foreground, let client, let host = selectedHost, let session = selectedSession else { return }
        let epoch = selectionEpoch; let cursor = projection.sequence
        streamTask = Task { [weak self] in
            do {
                let events = try await client.events(hostID: host, sessionID: session, fromSeq: cursor)
                for try await event in events {
                    guard let self, !Task.isCancelled, epoch == self.selectionEpoch else { return }
                    if event.event == "resync" || event.event == "error" { throw NexusError.resyncRequired("The connection needs a fresh snapshot") }
                    guard event.event == "view" else { continue }
                    let frame = try JSONDecoder().decode(JSONValue.self, from: Data(event.data.utf8))
                    switch self.projection.apply(event: frame) {
                    case .applied:
                        self.snapshot = .object(["schema_version": .integer(1), "session": .string(session), "seq": .integer(self.projection.sequence ?? 0), "view": self.projection.document ?? .null])
                        self.streaming = true; self.online = true; self.error = nil; self.lastUpdated = Date()
                    case .ignoredStale: break
                    case .needsSnapshot(let reason): throw NexusError.resyncRequired(reason)
                    }
                }
                guard !Task.isCancelled else { return }
                throw NexusError.resyncRequired("The event connection ended")
            } catch {
                guard let self, !Task.isCancelled, epoch == self.selectionEpoch else { return }
                self.streaming = false; self.online = false; self.handle(error); self.scheduleReconnect()
            }
        }
    }
    private func scheduleReconnect() {
        guard foreground, isPaired, selectedSession != nil else { return }
        let epoch = selectionEpoch
        streamTask = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(3)) } catch { return }
            guard let self, !Task.isCancelled, epoch == self.selectionEpoch else { return }
            // Release this retry task before refresh cancels the previous stream.
            self.streamTask = nil
            await self.refreshConversation()
        }
    }
    private func handle(_ failure: Error, write: Bool = false) {
        if case NexusError.httpStatus(let code, _) = failure, code == 401 {
            forget(); error = failure.localizedDescription; return
        }
        error = failure.localizedDescription + (write ? " Check the conversation before trying again; this action was not automatically retried." : "")
    }
}
