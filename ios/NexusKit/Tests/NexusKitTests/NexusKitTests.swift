import XCTest
@testable import NexusKit

final class NexusKitTests: XCTestCase {
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
        XCTAssertEqual(store.apply(event: .object(["schema_version": .integer(1), "session": .string("s"), "type": .string("patch"), "seq": .integer(7), "patch": .array([.object(["op": .string("add"), "path": .string("/a~1b/-"), "value": .string("y")])])])), .applied)
        XCTAssertEqual(store.document?["a/b"]?.array, [.string("x"), .string("y")])
        let prior = store.document
        XCTAssertEqual(store.apply(event: .object(["schema_version": .integer(1), "session": .string("s"), "type": .string("patch"), "seq": .integer(8), "patch": .array([.object(["op": .string("replace"), "path": .string("/missing"), "value": .null])])])), .needsSnapshot("patch rejected"))
        XCTAssertEqual(store.document, prior)
        XCTAssertEqual(store.sequence, 7)
    }
}
