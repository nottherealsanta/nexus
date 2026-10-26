// swift-tools-version: 5.9
import PackageDescription

/// Runs the app's connection/session state tests on macOS without a simulator.
let package = Package(
    name: "NexusAppState",
    platforms: [.macOS(.v14)],
    dependencies: [.package(path: "NexusKit")],
    targets: [
        .target(name: "NexusAppState", dependencies: [.product(name: "NexusKit", package: "NexusKit")],
                path: "NexusApp", exclude: ["NexusApp.swift", "RootView.swift", "ConversationView.swift", "StructuredValueView.swift", "Info.plist", "DictationRecorder.swift", "DictationView.swift", "LiveCallController.swift", "LiveCallView.swift"],
                sources: ["AppStore.swift", "Keychain.swift"]),
        .testTarget(name: "NexusAppStateTests", dependencies: ["NexusAppState", .product(name: "NexusKit", package: "NexusKit")])
    ]
)
