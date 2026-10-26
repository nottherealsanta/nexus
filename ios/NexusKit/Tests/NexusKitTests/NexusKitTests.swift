import XCTest
@testable import NexusKit

final class NexusKitTests: XCTestCase {
    func testPrivateGatewayRequiresHTTPS() throws {
        XCTAssertThrowsError(try GatewayClient(baseURL: URL(string: "http://100.64.0.1:8765")!))
        XCTAssertThrowsError(try GatewayClient(baseURL: URL(string: "http://100.64.0.1:8765")!, allowInsecureLoopback: true))
        XCTAssertNoThrow(try GatewayClient(baseURL: URL(string: "https://mac.example.ts.net")!))
        XCTAssertTrue(NexusError.httpStatus(401, "unauthorized").localizedDescription.contains("Pair this device again"))
    }
    func testJSONValueRoundTrip() throws {
        let value: JSONValue = .object(["name": .string("café"), "items": .array([.integer(2), .bool(true)])])
        XCTAssertEqual(try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(value)), value)
    }
    func testSSEIncrementalMultilineUnicode() throws {
        let text = "\u{feff}id: 9\r\nevent: update\r\ndata: café\r\ndata: line\r\n\r\n"
        var decoder = SSEDecoder(); var events: [ServerSentEvent] = []
        for byte in Data(text.utf8) { events += try decoder.feed(Data([byte])) }
        XCTAssertEqual(events, [ServerSentEvent(event: "update", data: "café\nline", id: "9", retry: nil)])
    }
    func testViewStoreAtomicPatchEscapingAndAppend() {
        var store = ViewStore()
        XCTAssertEqual(store.apply(snapshot: .object(["schema_version": .integer(1), "session": .string("s"), "seq": .integer(4), "view": .object(["a/b": .array([.string("x")])])])), .applied)
        XCTAssertEqual(store.apply(event: .object(["schema_version": .integer(1), "session": .string("s"), "seq": .integer(7), "ops": .array([.object(["op": .string("add"), "path": .string("/a~1b/-"), "value": .string("y")])])])), .applied)
        XCTAssertEqual(store.document?["a/b"]?.array, [.string("x"), .string("y")])
        let prior = store.document
        XCTAssertEqual(store.apply(event: .object(["schema_version": .integer(1), "session": .string("s"), "seq": .integer(8), "ops": .array([.object(["op": .string("add"), "path": .string("/staged"), "value": .string("must roll back")]), .object(["op": .string("replace"), "path": .string("/missing"), "value": .null])])])), .needsSnapshot("patch rejected"))
        XCTAssertEqual(store.document, prior)
        XCTAssertEqual(store.sequence, 7)
    }
}

/// Echoes the request through URLSession without a live gateway or credentials.
private final class RequestEchoProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(contentsOf: buffer.prefix(count))
            }
        }
        let payload = (try? JSONDecoder().decode(JSONValue.self, from: body)) ?? .null
        let echo: JSONValue = .object([
            "payload": payload, "path": .string(request.url!.path),
            "method": .string(request.httpMethod ?? ""),
            "authorization": .string(request.value(forHTTPHeaderField: "Authorization") ?? "")])
        let data = try! JSONEncoder().encode(echo)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

extension NexusKitTests {
    func testSessionCommandsMatchHostContract() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RequestEchoProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let client = try GatewayClient(baseURL: URL(string: "https://mac.example.ts.net")!, token: "test-device", session: session)
        let created = try await client.createSession(hostID: "workspace", sessionID: "new-session")
        XCTAssertEqual(created["path"]?.string, "/v1/hosts/workspace/command")
        XCTAssertEqual(created["method"]?.string, "POST")
        XCTAssertEqual(created["authorization"]?.string, "Bearer test-device")
        XCTAssertEqual(created["payload"], .object(["type": .string("SessionOpen"), "session": .string("new-session"), "create": .bool(true)]))
        let requestID = UUID()
        let sent = try await client.enqueue(hostID: "workspace", sessionID: "new-session", content: "Hello café", requestID: requestID)
        XCTAssertEqual(sent["payload"], .object([
            "type": .string("SessionEnqueue"), "session": .string("new-session"), "content": .string("Hello café"),
            "mode": .string("queue"), "client_request_id": .string(requestID.uuidString)]))
        do {
            _ = try await client.createSession(hostID: "workspace", sessionID: "../escape")
            XCTFail("Invalid session identifiers must fail before making a request")
        } catch { XCTAssertEqual(error as? NexusError, .invalidIdentifier) }
    }
}

extension NexusKitTests {
    func testCanonicalHostFramesReplayToSnapshot() throws {
        // Matches host/facade.py subscribe_web: `ops`, no synthetic `type` tag;
        // sequence gaps are valid because other durable records share the cursor.
        let original = #"{"schema_version":1,"session":"s","seq":4,"view":{"turns":[{"messages":[{"blocks":[{"kind":"text","text":"Hello"}]}]}],"phase":"running"}}"#
        let frame = #"{"schema_version":1,"session":"s","seq":9,"ops":[{"op":"append","path":"/turns/0/messages/0/blocks/0/text","value":" café"},{"op":"replace","path":"/phase","value":"idle"}]}"#
        let final = #"{"schema_version":1,"session":"s","seq":9,"view":{"turns":[{"messages":[{"blocks":[{"kind":"text","text":"Hello café"}]}]}],"phase":"idle"}}"#
        var replay = ViewStore(); var fresh = ViewStore()
        XCTAssertEqual(replay.apply(snapshot: try JSONDecoder().decode(JSONValue.self, from: Data(original.utf8))), .applied)
        var decoder = SSEDecoder()
        let wire = "id: 9\nevent: view\ndata: \(frame)\n\n"
        var events: [ServerSentEvent] = []
        for byte in wire.utf8 { events += try decoder.feed(Data([byte])) }
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(replay.apply(event: try JSONDecoder().decode(JSONValue.self, from: Data(events[0].data.utf8))), .applied)
        XCTAssertEqual(fresh.apply(snapshot: try JSONDecoder().decode(JSONValue.self, from: Data(final.utf8))), .applied)
        XCTAssertEqual(replay.document, fresh.document)
        XCTAssertEqual(replay.sequence, fresh.sequence)
        XCTAssertEqual(replay.apply(event: try JSONDecoder().decode(JSONValue.self, from: Data(frame.utf8))), .ignoredStale)
        let lastGood = replay.document
        XCTAssertEqual(replay.apply(event: .object(["schema_version": .integer(1), "session": .string("s"), "seq": .integer(10), "resync": .bool(true)])), .needsSnapshot("invalid patch envelope"))
        XCTAssertEqual(replay.document, lastGood)
    }
    func testSSEBoundsWholeMultilineEventAndAllowsBatchedEvents() throws {
        var bounded = SSEDecoder(maximumLineBytes: 20, maximumEventBytes: 5)
        _ = try bounded.feed(Data("data: abc\n".utf8))
        XCTAssertThrowsError(try bounded.feed(Data("data: def\n".utf8)))
        var batched = SSEDecoder(maximumLineBytes: 10)
        let events = try batched.feed(Data("data: a\n\ndata: b\n\ndata: c\n\n".utf8))
        XCTAssertEqual(events.map(\.data), ["a", "b", "c"])
    }
    func testGatewayAddressRejectsCredentialsQueriesAndFragments() {
        for address in ["https://user:secret@mac.ts.net", "https://mac.ts.net?token=x", "https://mac.ts.net/#fragment"] {
            XCTAssertThrowsError(try GatewayClient(baseURL: URL(string: address)!))
        }
    }
    func testArrayInsertionAtEndAndSnapshotSessionReset() {
        var store = ViewStore()
        XCTAssertEqual(store.apply(snapshot: .object(["schema_version": .integer(1), "session": .string("first"), "seq": .integer(10), "view": .array([.integer(1)])])), .applied)
        XCTAssertEqual(store.apply(event: .object(["schema_version": .integer(1), "session": .string("first"), "seq": .integer(11), "ops": .array([.object(["op": .string("add"), "path": .string("/1"), "value": .integer(2)])])])), .applied)
        XCTAssertEqual(store.document, .array([.integer(1), .integer(2)]))
        XCTAssertEqual(store.apply(snapshot: .object(["schema_version": .integer(1), "session": .string("second"), "seq": .integer(1), "view": .object([:])])), .applied)
        XCTAssertEqual(store.sessionID, "second")
    }
}
