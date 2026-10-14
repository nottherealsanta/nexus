import XCTest

final class NexusUITests: XCTestCase {
    func testPairingFormShowsValidationFields() throws {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.textFields["gatewayURL"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.secureTextFields["pairingCode"].exists)
        XCTAssertTrue(app.buttons["Pair device"].exists)
    }
}
