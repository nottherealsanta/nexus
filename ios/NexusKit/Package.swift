// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "NexusKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "NexusKit", targets: ["NexusKit"])],
    targets: [
        .target(name: "NexusKit"),
        .testTarget(name: "NexusKitTests", dependencies: ["NexusKit"])
    ]
)
