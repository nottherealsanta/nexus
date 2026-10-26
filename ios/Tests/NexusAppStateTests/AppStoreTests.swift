import XCTest
import NexusKit
@testable import NexusAppState

private final class MockGateway: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    private static var calls: [String] = []
    private static var commandFailure = false
    private static var failHosts = false
    private static var streamFrame: String?
    private static var pendingApproval = false
    private static var payloads: [JSONValue] = []
    private var stopped = false
    private let stateLock = NSLock()
    static func reset(failCommand: Bool = false, unauthorized: Bool = false, event: String? = nil, pending: Bool = false) {
        lock.lock(); defer { lock.unlock() }
        calls = []; commandFailure = failCommand; failHosts = unauthorized; streamFrame = event; pendingApproval = pending; payloads = []
    }
    static var commandCount: Int {
        lock.lock(); defer { lock.unlock() }
        return calls.filter { $0.hasSuffix("/command") }.count
    }
    static var commands: [JSONValue] {
        lock.lock(); defer { lock.unlock() }; return payloads
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        Self.lock.lock(); Self.calls.append(path)
        let failCommand = Self.commandFailure; let unauthorized = Self.failHosts; let frame = Self.streamFrame; let pending = Self.pendingApproval
        Self.lock.unlock()
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }; body.append(contentsOf: buffer.prefix(count))
            }
        }
        let payload = (try? JSONDecoder().decode(JSONValue.self, from: body)) ?? .null
        let data: String
        var status = 200
        if path == "/v1/hosts" {
            status = unauthorized ? 401 : 200
            data = unauthorized ? #"{"reason":"revoked"}"# : #"[{"host_id":"workspace","label":"Project"}]"#
        } else if path.hasSuffix("/sessions") {
            data = #"{"sessions":[{"id":"a","title":"First","state":"idle"},{"id":"b","title":"Second","state":"idle"}],"truncated":true}"#
        } else if path.hasSuffix("/command") {
            status = failCommand ? 503 : 200
            Self.lock.lock(); Self.payloads.append(payload); Self.lock.unlock()
            if failCommand { data = #"{"reason":"host unavailable"}"# }
            else if payload["type"]?.string == "SessionOpen" {
                data = "{\"type\":\"SessionOpenResult\",\"session\":{\"id\":\"\(payload["session"]?.string ?? "")\"}}"
            } else if payload["type"]?.string == "PermissionResolve" {
                data = #"{"type":"PermissionResolveResult","session":"a","request_id":"approval","resolved":true}"#
            } else if payload["type"]?.string == "SessionCancel" {
                data = #"{"type":"SessionCancelResult","session":"a","cancelled":true,"dropped":0}"#
            } else { data = #"{"type":"SessionEnqueueResult","session":"a","queued_id":"q"}"# }
        } else if path.hasSuffix("/view") {
            let id = path.split(separator: "/").dropLast().last.map(String.init) ?? "a"
            let permissions = pending ? #"[{"id":"approval","status":"pending","tool":"bash","preview":"echo hello"}]"# : "[]"
            data = "{\"schema_version\":1,\"session\":\"\(id)\",\"seq\":1,\"view\":{\"phase\":\"idle\",\"turns\":[],\"permissions\":\(permissions)}}"
        } else if path.hasSuffix("/events") { data = frame ?? "" }
        else { data = "" }
        let delay = path.contains("/slow/") || path.hasSuffix("/command") ? 0.15 : 0.0
        let responseStatus = status
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [self] in
            stateLock.lock(); defer { stateLock.unlock() }
            guard !stopped else { return }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: responseStatus, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(data.utf8))
            if !path.hasSuffix("/events") { client?.urlProtocolDidFinishLoading(self) }
        }
    }
    override func stopLoading() { stateLock.lock(); stopped = true; stateLock.unlock() }
}

final class AppStoreTests: XCTestCase {
    @MainActor private func makeStore(scopes: [String] = ["read", "prompt", "approve"]) async -> (AppStore, UserDefaults, URLSession) {
        let defaults = UserDefaults(suiteName: "nexus-ios-tests-\(UUID().uuidString)")!
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockGateway.self]
        let transport = URLSession(configuration: configuration)
        let credential = PairingCredential(token: "test", scopes: scopes, deviceID: "phone", boundURL: "https://mac.example.ts.net")
        let store = AppStore(defaults: defaults, credential: credential, transport: transport, saveCredential: { _ in }, removeCredential: {})
        await store.setForeground(false)
        await store.refresh()
        return (store, defaults, transport)
    }
    @MainActor func testSessionCreationSelectsConfirmedNewSession() async {
        MockGateway.reset()
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.createSession()
        XCTAssertNotNil(store.selectedSession)
        XCTAssertEqual(store.snapshot?["session"]?.string, store.selectedSession)
        XCTAssertEqual(MockGateway.commands.first?["type"]?.string, "SessionOpen")
        XCTAssertEqual(MockGateway.commands.first?["create"]?.bool, true)
        XCTAssertFalse(store.busy)
    }
    @MainActor func testStopRetainsQueueAndApprovalUsesHostDecision() async {
        MockGateway.reset(pending: true)
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.selectSession("a")
        await store.stopTurn()
        XCTAssertEqual(MockGateway.commands.first?["drop_queue"]?.bool, false)
        await store.resolvePermission("approval", decision: "allow_once")
        XCTAssertEqual(MockGateway.commands.last?["type"]?.string, "PermissionResolve")
        XCTAssertEqual(MockGateway.commands.last?["decision"]?.string, "allow_once")
        XCTAssertEqual(MockGateway.commands.last?["request_id"]?.string, "approval")
    }
    @MainActor func testForgottenDeviceCannotBeRepopulatedByLateSnapshot() async throws {
        MockGateway.reset()
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        let old = Task { await store.selectSession("slow") }
        try await Task.sleep(for: .milliseconds(30))
        store.forget()
        await old.value
        XCTAssertNil(store.snapshot)
        XCTAssertNil(store.selectedSession)
        XCTAssertTrue(store.hosts.isEmpty)
        XCTAssertFalse(store.loading)
        XCTAssertFalse(store.isPaired)
    }
    @MainActor func testResyncAutomaticallyRefetchesSnapshotAndReconnects() async throws {
        MockGateway.reset(event: "event: resync\ndata: {\"reason\":\"host_unavailable\"}\n\n")
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.selectSession("a")
        await store.setForeground(true)
        for _ in 0..<100 {
            if store.error != nil { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertFalse(store.online)
        let frame = #"{"schema_version":1,"session":"a","seq":4,"ops":[{"op":"replace","path":"/phase","value":"running"}]}"#
        MockGateway.reset(event: "event: view\ndata: \(frame)\n\n")
        for _ in 0..<250 {
            if store.streaming { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertTrue(store.online)
        XCTAssertTrue(store.streaming)
        XCTAssertEqual(store.snapshot?["seq"]?.int, 4)
        await store.setForeground(false)
    }
    @MainActor func testLiveHostPatchUpdatesSelectedConversation() async throws {
        let frame = #"{"schema_version":1,"session":"a","seq":3,"ops":[{"op":"replace","path":"/phase","value":"running"}]}"#
        MockGateway.reset(event: "id: 3\nevent: view\ndata: \(frame)\n\n")
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.selectSession("a")
        await store.setForeground(true)
        for _ in 0..<100 {
            if store.snapshot?["seq"]?.int == 3 { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(store.conversation?["phase"]?.string, "running")
        XCTAssertEqual(store.snapshot?["seq"]?.int, 3)
        XCTAssertTrue(store.streaming)
        await store.setForeground(false)
        XCTAssertFalse(store.streaming)
    }
    @MainActor func testRefreshKeepsSelectionAndPerSessionDrafts() async {
        MockGateway.reset()
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        XCTAssertEqual(store.selectedHost, "workspace")
        XCTAssertTrue(store.truncated)
        await store.selectSession("a")
        store.updateDraft("Draft for A")
        await store.selectSession("b")
        store.updateDraft("Draft for B")
        await store.reload()
        XCTAssertEqual(store.selectedSession, "b")
        XCTAssertEqual(store.draft, "Draft for B")
        await store.selectSession("a")
        XCTAssertEqual(store.draft, "Draft for A")
        XCTAssertEqual(store.snapshot?["session"]?.string, "a")
    }
    @MainActor func testLateSnapshotCannotReplaceNewSelection() async throws {
        MockGateway.reset()
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        let old = Task { await store.selectSession("slow") }
        try await Task.sleep(for: .milliseconds(30))
        await store.selectSession("b")
        await old.value
        XCTAssertEqual(store.selectedSession, "b")
        XCTAssertEqual(store.snapshot?["session"]?.string, "b")
        XCTAssertFalse(store.loading)
    }
    @MainActor func testFailedSendPreservesDraftAndDoesNotRetry() async {
        MockGateway.reset(failCommand: true)
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.selectSession("a")
        store.updateDraft("Keep this message")
        await store.send()
        XCTAssertEqual(store.draft, "Keep this message")
        XCTAssertEqual(MockGateway.commandCount, 1)
        XCTAssertTrue(store.error?.contains("not automatically retried") == true)
        XCTAssertFalse(store.busy)
    }
    @MainActor func testAcceptedSendClearsOnlyItsOriginalDraftAfterSwitch() async throws {
        MockGateway.reset()
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.selectSession("a")
        store.updateDraft("Sent from A")
        let sending = Task { await store.send() }
        try await Task.sleep(for: .milliseconds(30))
        await store.selectSession("b")
        store.updateDraft("Keep B")
        await sending.value
        XCTAssertEqual(store.draft, "Keep B")
        await store.selectSession("a")
        XCTAssertEqual(store.draft, "")
        XCTAssertEqual(MockGateway.commandCount, 1)
    }
    @MainActor func testReadOnlyDeviceCannotWrite() async {
        MockGateway.reset()
        let (store, defaults, session) = await makeStore(scopes: ["read"])
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.selectSession("a")
        store.updateDraft("Local only")
        await store.send(); await store.createSession()
        XCTAssertEqual(MockGateway.commandCount, 0)
        XCTAssertEqual(store.draft, "Local only")
    }
    @MainActor func testRevocationClearsSelectedContent() async {
        MockGateway.reset()
        let (store, defaults, session) = await makeStore()
        defer { session.invalidateAndCancel(); defaults.removeObject(forKey: "nexus.drafts") }
        await store.selectSession("a")
        MockGateway.reset(unauthorized: true)
        await store.refresh()
        XCTAssertFalse(store.isPaired)
        XCTAssertNil(store.snapshot)
        XCTAssertNil(store.selectedSession)
        XCTAssertTrue(store.hosts.isEmpty)
        XCTAssertTrue(store.error?.contains("Pair this device again") == true)
    }
}
