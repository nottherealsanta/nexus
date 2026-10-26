import Foundation

public enum JSONValue: Codable, Sendable, Equatable {
    case null, bool(Bool), integer(Int), number(Double), string(String), array([JSONValue]), object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int.self) { self = .integer(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .integer(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
    public subscript(_ key: String) -> JSONValue? { if case .object(let o) = self { return o[key] }; return nil }
    public var string: String? { if case .string(let v) = self { return v }; return nil }
    public var int: Int? { if case .integer(let v) = self { return v }; return nil }
    public var bool: Bool? { if case .bool(let v) = self { return v }; return nil }
    public var array: [JSONValue]? { if case .array(let v) = self { return v }; return nil }
    public var object: [String: JSONValue]? { if case .object(let v) = self { return v }; return nil }
}

public struct GatewayHost: Codable, Sendable, Equatable {
    public let host_id: String
    public let workspace: String?
    public let label: String?
    public let state: String?
    public init(host_id: String, workspace: String? = nil, label: String? = nil, state: String? = nil) {
        self.host_id = host_id; self.workspace = workspace; self.label = label; self.state = state
    }
}
public struct PairingResponse: Codable, Sendable, Equatable {
    public let device_id: String; public let token: String; public let scopes: [String]
    public let expires_at: Double; public let gateway_id: String
}
public struct ErrorResult: Codable, Sendable, Equatable { public let reason: String }
public struct ServerSentEvent: Sendable, Equatable { public let event: String?; public let data: String; public let id: String?; public let retry: Int? }

public enum NexusError: Error, Sendable, Equatable {
    case invalidURL, invalidIdentifier, responseTooLarge, httpStatus(Int, String), invalidResponse(String), resyncRequired(String)
}

public enum GatewayProtocol {
    public static let maxIdentifierLength = 128
    public static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= maxIdentifierLength && value != "." && value != ".." && value.rangeOfCharacter(from: CharacterSet(charactersIn: "/%\\?#\u{0}\r\n")) == nil
    }
}

extension NexusError: LocalizedError {
    public var errorDescription: String? {
        switch self {
        case .invalidURL: return "Enter a valid HTTPS gateway address from Tailscale Serve."
        case .invalidIdentifier: return "The workspace or session identifier is invalid."
        case .responseTooLarge: return "The gateway response exceeds the device limit."
        case .httpStatus(let status, let reason):
            if status == 401 { return "Your device pairing has expired or was revoked. Pair this device again." }
            if (300..<400).contains(status) { return "Gateway redirects are not accepted. Enter the final HTTPS address from Tailscale Serve." }
            if status == 403 { return "This device does not have permission for this action. \(reason)" }
            return "Gateway request failed (\(status)): \(reason)"
        case .invalidResponse(let reason): return "The gateway returned an unexpected response: \(reason)"
        case .resyncRequired(let reason): return "Refresh this session to reconnect: \(reason)"
        }
    }
}
