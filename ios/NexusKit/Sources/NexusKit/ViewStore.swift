import Foundation

public enum ViewUpdate: Sendable, Equatable { case applied, ignoredStale, needsSnapshot(String) }

/// Maintains one session projection. Updates are staged on a copy and committed only on success.
public struct ViewStore: Sendable {
    public private(set) var document: JSONValue?
    public private(set) var sequence: Int?
    public private(set) var sessionID: String?
    public let maximumOperations: Int
    public let maximumDepth: Int
    public let maximumEncodedBytes: Int
    public init(maximumOperations: Int = 10_000, maximumDepth: Int = 128, maximumEncodedBytes: Int = 32 * 1024 * 1024) {
        self.maximumOperations = maximumOperations; self.maximumDepth = maximumDepth; self.maximumEncodedBytes = maximumEncodedBytes
    }
    @discardableResult public mutating func apply(snapshot: JSONValue) -> ViewUpdate {
        guard let obj = snapshot.object else { return .needsSnapshot("snapshot must be an object") }
        guard obj["schema_version"]?.int == 1,
              let seq = obj["seq"]?.int, seq >= 0,
              let sid = obj["session"]?.string, GatewayProtocol.validIdentifier(sid),
              let view = obj["view"] else { return .needsSnapshot("invalid snapshot envelope") }
        if let old = sequence, seq < old { return .ignoredStale }
        guard bounded(view) else { return .needsSnapshot("snapshot exceeds bounds") }
        document = view; sequence = seq; sessionID = sid; return .applied
    }
    @discardableResult public mutating func apply(event: JSONValue) -> ViewUpdate {
        guard let obj = event.object, let seq = obj["seq"]?.int, seq >= 0,
              obj["schema_version"]?.int == 1,
              let sid = obj["session"]?.string, GatewayProtocol.validIdentifier(sid),
              let type = obj["type"]?.string, type == "patch" else { return .needsSnapshot("invalid patch envelope") }
        if let current = sessionID, sid != current { return .needsSnapshot("session changed") }
        if let old = sequence, seq <= old { return .ignoredStale }
        guard let oldDoc = document else { return .needsSnapshot("snapshot required") }
        let ops = obj["patch"]?.array
        guard let ops, ops.count <= maximumOperations else { return .needsSnapshot("invalid or oversized patch") }
        var candidate = oldDoc
        do {
            for item in ops {
                guard let op = item.object, let kind = op["op"]?.string, let path = op["path"]?.string else { throw NexusError.invalidResponse("malformed operation") }
                candidate = try Self.modify(candidate, kind: kind, path: path, value: op["value"], depthLimit: maximumDepth)
            }
        } catch { return .needsSnapshot("patch rejected") }
        guard bounded(candidate) else { return .needsSnapshot("patched view exceeds bounds") }
        document = candidate; sequence = seq; sessionID = sid; return .applied
    }
    private func bounded(_ value: JSONValue) -> Bool {
        guard let data = try? JSONEncoder().encode(value), data.count <= maximumEncodedBytes else { return false }
        func depth(_ v: JSONValue, _ d: Int) -> Bool {
            if d > maximumDepth { return false }
            switch v { case .array(let a): return a.allSatisfy { depth($0, d + 1) }; case .object(let o): return o.values.allSatisfy { depth($0, d + 1) }; default: return true }
        }
        return depth(value, 0)
    }
    private static func modify(_ root: JSONValue, kind: String, path: String, value: JSONValue?, depthLimit: Int) throws -> JSONValue {
        guard path.isEmpty || path.first == "/" else { throw NexusError.invalidResponse("invalid pointer") }
        let parts = path.isEmpty ? [] : try path.dropFirst().split(separator: "/", omittingEmptySubsequences: false).map { raw -> String in
            let s = String(raw); var out = ""; var i = s.startIndex
            while i < s.endIndex {
                if s[i] == "~" { let n = s.index(after: i); guard n < s.endIndex else { throw NexusError.invalidResponse("bad pointer escape") }; if s[n] == "0" { out += "~" } else if s[n] == "1" { out += "/" } else { throw NexusError.invalidResponse("bad pointer escape") }; i = s.index(after: n) }
                else { out.append(s[i]); i = s.index(after: i) }
            }; return out
        }
        guard parts.count <= depthLimit else { throw NexusError.responseTooLarge }
        func update(_ node: JSONValue, _ index: Int) throws -> JSONValue {
            if index == parts.count {
                if kind == "replace" { guard value != nil else { throw NexusError.invalidResponse("missing value") }; return value! }
                if kind == "append" { guard case .string(let old) = root, case .string(let suffix)? = value else { throw NexusError.invalidResponse("append requires strings") }; return .string(old + suffix) }
                if kind == "remove" { throw NexusError.invalidResponse("cannot remove root") }
                throw NexusError.invalidResponse("unsupported operation")
            }
            let key = parts[index]
            if case .object(var object) = node {
                if index == parts.count - 1 {
                    switch kind { case "add": guard let value else { throw NexusError.invalidResponse("missing value") }; object[key] = value
                    case "replace": guard object[key] != nil, let value else { throw NexusError.invalidResponse("replace target missing") }; object[key] = value
                    case "append": guard case .string(let old)? = object[key], case .string(let suffix)? = value else { throw NexusError.invalidResponse("append requires strings") }; object[key] = .string(old + suffix)
                    case "remove": guard object.removeValue(forKey: key) != nil else { throw NexusError.invalidResponse("remove target missing") }
                    default: throw NexusError.invalidResponse("unsupported operation") }
                } else { guard let child = object[key] else { throw NexusError.invalidResponse("path missing") }; object[key] = try update(child, index + 1) }
                return .object(object)
            }
            if case .array(var array) = node {
                if index == parts.count - 1 {
                    if kind == "add", key == "-" { guard let value else { throw NexusError.invalidResponse("missing value") }; array.append(value); return .array(array) }
                    guard let n = Int(key), n >= 0, n < array.count else { throw NexusError.invalidResponse("array index invalid") }
                    switch kind { case "add": guard let value else { throw NexusError.invalidResponse("missing value") }; array.insert(value, at: n)
                    case "replace": guard let value else { throw NexusError.invalidResponse("missing value") }; array[n] = value
                    case "append": guard case .string(let old) = array[n], case .string(let suffix)? = value else { throw NexusError.invalidResponse("append requires strings") }; array[n] = .string(old + suffix)
                    case "remove": array.remove(at: n)
                    default: throw NexusError.invalidResponse("unsupported operation") }
                } else { guard let n = Int(key), n >= 0, n < array.count else { throw NexusError.invalidResponse("array index invalid") }; array[n] = try update(array[n], index + 1) }
                return .array(array)
            }
            throw NexusError.invalidResponse("path traverses scalar")
        }
        return try update(root, 0)
    }
}
