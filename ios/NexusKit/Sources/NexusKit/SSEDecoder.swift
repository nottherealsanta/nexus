import Foundation

/// Incremental UTF-8 SSE parser. Feed arbitrary byte chunks; finish flushes the final event.
public struct SSEDecoder: Sendable {
    private var bytes = Data()
    private var eventName: String?
    private var dataLines: [String] = []
    private var lastID: String?
    private var retry: Int?
    private var firstLine = true
    private var eventBytes = 0
    public let maximumLineBytes: Int
    public let maximumEventBytes: Int
    public init(maximumLineBytes: Int = 8 * 1024 * 1024, maximumEventBytes: Int = 8 * 1024 * 1024) {
        self.maximumLineBytes = maximumLineBytes; self.maximumEventBytes = maximumEventBytes
    }

    public mutating func feed(_ chunk: Data) throws -> [ServerSentEvent] {
        bytes.append(chunk)
        var events: [ServerSentEvent] = []
        while let idx = bytes.firstIndex(of: 10) {
            guard bytes.distance(from: bytes.startIndex, to: idx) <= maximumLineBytes else { throw NexusError.responseTooLarge }
            var line = Data(bytes[..<idx]); bytes.removeSubrange(...idx)
            if line.last == 13 { line.removeLast() }
            guard let text = String(data: line, encoding: .utf8) else { throw NexusError.invalidResponse("invalid UTF-8 SSE") }
            if firstLine { firstLine = false; if text.hasPrefix("\u{feff}") { let clean = String(text.dropFirst()); try parse(clean, into: &events); continue } }
            try parse(text, into: &events)
        }
        if bytes.count > maximumLineBytes { throw NexusError.responseTooLarge }
        return events
    }
    public mutating func finish() throws -> [ServerSentEvent] {
        var out = try feed(Data([10]))
        if !bytes.isEmpty {
            let line = bytes; bytes.removeAll()
            guard let text = String(data: line, encoding: .utf8) else { throw NexusError.invalidResponse("invalid UTF-8 SSE") }
            try parse(text, into: &out)
        }
        dispatch(into: &out); return out
    }
    private mutating func parse(_ line: String, into output: inout [ServerSentEvent]) throws {
        if line.isEmpty { dispatch(into: &output); return }
        if line.hasPrefix(":") { return }
        let parts = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        let field = String(parts[0]); var value = parts.count > 1 ? String(parts[1]) : ""
        if value.first == " " { value.removeFirst() }
        switch field {
        case "event": eventName = value
        case "data":
            eventBytes += value.utf8.count + 1
            guard eventBytes <= maximumEventBytes else { throw NexusError.responseTooLarge }
            dataLines.append(value)
        case "id": if !value.contains("\0") { lastID = value }
        case "retry": if let n = Int(value), n >= 0 { retry = n }
        default: break
        }
    }
    private mutating func dispatch(into output: inout [ServerSentEvent]) {
        if !dataLines.isEmpty { output.append(ServerSentEvent(event: eventName, data: dataLines.joined(separator: "\n"), id: lastID, retry: retry)) }
        eventName = nil; dataLines.removeAll(keepingCapacity: true); retry = nil; eventBytes = 0
    }
}
