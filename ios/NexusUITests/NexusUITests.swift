import XCTest

final class NexusUITests: XCTestCase {
    @MainActor func testPairingFormShowsValidationFields() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        XCTAssertTrue(app.textFields["gatewayURL"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.secureTextFields["pairingCode"].exists)
        XCTAssertTrue(app.buttons["Pair device"].exists)
        XCTAssertFalse(app.buttons["Pair device"].isEnabled)
    }
    @MainActor func testInsecureGatewayIsRejectedBeforePairing() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        let address = app.textFields["gatewayURL"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        address.tap(); address.typeText("http://100.64.0.1:8765")
        let code = app.secureTextFields["pairingCode"]
        code.tap(); code.typeText("test-code")
        app.buttons["Pair device"].tap()
        app.swipeUp()
        XCTAssertTrue(app.staticTexts["Enter a valid HTTPS gateway address from Tailscale Serve."].waitForExistence(timeout: 5))
    }
}
