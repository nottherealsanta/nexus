import Foundation

public actor GatewayClient {
    public let baseURL: URL
    private let token: String?
    private let session: URLSession
    private let maxResponseBytes = 32 * 1024 * 1024

    public init(baseURL: URL, token: String? = nil, allowInsecureLoopback: Bool = false, session: URLSession = .shared) throws {
        guard let scheme = baseURL.scheme?.lowercased(), scheme == "https" || (scheme == "http" && allowInsecureLoopback && Self.isLoopback(baseURL.host)) else { throw NexusError.invalidURL }
        guard let parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              let host = parts.host, !host.isEmpty, parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil else { throw NexusError.invalidURL }
        self.baseURL = baseURL; self.token = token; self.session = session
    }
    private static func isLoopback(_ host: String?) -> Bool { guard let h = host?.lowercased() else { return false }; return ["localhost", "127.0.0.1", "::1"].contains(h) }
    private func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw NexusError.invalidURL }
        let prefix = c.path.hasSuffix("/") ? String(c.path.dropLast()) : c.path
        c.path = prefix + path; c.queryItems = query.isEmpty ? nil : query
        guard let result = c.url else { throw NexusError.invalidURL }; return result
    }
    private func request(_ method: String, path: String, body: Data? = nil, authenticated: Bool = true, query: [URLQueryItem] = [], contentType: String = "application/json") async throws -> Data {
        var req = URLRequest(url: try url(path, query: query)); req.httpMethod = method; req.httpBody = body; req.timeoutInterval = 20
        if body != nil { req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if authenticated, let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, response) = try await session.data(for: req, delegate: GatewayRedirectPolicy())
        guard data.count <= maxResponseBytes else { throw NexusError.responseTooLarge }
        guard let http = response as? HTTPURLResponse else { throw NexusError.invalidResponse("non-HTTP response") }
        guard (200..<300).contains(http.statusCode) else {
            let reason = (try? JSONDecoder().decode(ErrorResult.self, from: data).reason) ?? "request failed"
            throw NexusError.httpStatus(http.statusCode, reason)
        }
        return data
    }
    private func json<T: Decodable>(_ type: T.Type, method: String, path: String, value: JSONValue? = nil) async throws -> T {
        let body = try value.map { try JSONEncoder().encode($0) }
        return try JSONDecoder().decode(type, from: await request(method, path: path, body: body))
    }
    private func hostPath(_ host: String, _ suffix: String) throws -> String {
        guard GatewayProtocol.validIdentifier(host) else { throw NexusError.invalidIdentifier }
        return "/v1/hosts/\(host)\(suffix)"
    }
    public func health() async throws -> JSONValue { try await json(JSONValue.self, method: "GET", path: "/healthz", value: nil) }
    public func hosts() async throws -> [GatewayHost] { try await json([GatewayHost].self, method: "GET", path: "/v1/hosts", value: nil) }
    public func pair(code: String, deviceName: String) async throws -> PairingResponse {
        try await json(PairingResponse.self, method: "POST", path: "/v1/pair", value: .object(["code": .string(code), "device_name": .string(deviceName)]))
    }
    public func me() async throws -> JSONValue { try await json(JSONValue.self, method: "GET", path: "/v1/me", value: nil) }
    public func sessions(hostID: String) async throws -> JSONValue { try await json(JSONValue.self, method: "GET", path: try hostPath(hostID, "/sessions"), value: nil) }
    public func snapshot(hostID: String, sessionID: String) async throws -> JSONValue {
        guard GatewayProtocol.validIdentifier(sessionID) else { throw NexusError.invalidIdentifier }
        return try await json(JSONValue.self, method: "GET", path: try hostPath(hostID, "/sessions/\(sessionID)/view"), value: nil)
    }
    public func command(hostID: String, payload: JSONValue) async throws -> JSONValue {
        try await json(JSONValue.self, method: "POST", path: try hostPath(hostID, "/command"), value: payload)
    }
    /// Commands use the host protocol tags and fields without gateway translation.
    public func createSession(hostID: String, sessionID: String) async throws -> JSONValue {
        guard GatewayProtocol.validIdentifier(sessionID) else { throw NexusError.invalidIdentifier }
        return try await command(hostID: hostID, payload: .object([
            "type": .string("SessionOpen"), "session": .string(sessionID), "create": .bool(true)]))
    }
    public func enqueue(hostID: String, sessionID: String, content: String, requestID: UUID) async throws -> JSONValue {
        guard GatewayProtocol.validIdentifier(sessionID), !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw NexusError.invalidResponse("Select a session and enter a message") }
        return try await command(hostID: hostID, payload: .object([
            "type": .string("SessionEnqueue"), "session": .string(sessionID), "content": .string(content),
            "mode": .string("queue"), "client_request_id": .string(requestID.uuidString)]))
    }
    public func voice(hostID: String, requestID: UUID, wav: Data) async throws -> JSONValue {
        guard wav.count <= 8 * 1024 * 1024 else { throw NexusError.responseTooLarge }
        let path = try hostPath(hostID, "/voice")
        let (_, response) = try await session.data(for: {
            var r = URLRequest(url: try url(path, query: [URLQueryItem(name: "request_id", value: requestID.uuidString)])); r.httpMethod = "POST"; r.httpBody = wav; r.setValue("audio/wav", forHTTPHeaderField: "Content-Type"); if let token { r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }; return r
        }())
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw NexusError.invalidResponse("voice request failed") }
        return .null
    }
    public func events(hostID: String, sessionID: String, fromSeq: Int? = nil) throws -> AsyncThrowingStream<ServerSentEvent, Error> {
        guard GatewayProtocol.validIdentifier(hostID), GatewayProtocol.validIdentifier(sessionID) else { throw NexusError.invalidIdentifier }
        let endpoint = try url("/v1/hosts/\(hostID)/sessions/\(sessionID)/events", query: fromSeq.map { [URLQueryItem(name: "from_seq", value: String($0))] } ?? [])
        var request = URLRequest(url: endpoint); request.setValue("text/event-stream", forHTTPHeaderField: "Accept"); if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let session = self.session
        return AsyncThrowingStream(bufferingPolicy: .bufferingOldest(32)) { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await session.bytes(for: request, delegate: GatewayRedirectPolicy())
                    guard let http = response as? HTTPURLResponse else { throw NexusError.invalidResponse("event stream failed") }
                    guard (200..<300).contains(http.statusCode) else { throw NexusError.httpStatus(http.statusCode, "Event connection was refused") }
                    var decoder = SSEDecoder(maximumLineBytes: 512 * 1024, maximumEventBytes: 512 * 1024)
                    for try await byte in bytes {
                        for event in try decoder.feed(Data([byte])) {
                            switch continuation.yield(event) {
                            case .enqueued: break
                            case .dropped: throw NexusError.resyncRequired("Live updates exceeded the device buffer")
                            case .terminated: return
                            @unknown default: return
                            }
                        }
                        if Task.isCancelled { break }
                    }
                    for event in try decoder.finish() {
                        if case .dropped = continuation.yield(event) { throw NexusError.resyncRequired("Live updates exceeded the device buffer") }
                    }
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

/// The pairing origin is a credential boundary; proxies must serve it directly.
private final class GatewayRedirectPolicy: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
