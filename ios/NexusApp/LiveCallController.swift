import Foundation
import AVFoundation

@MainActor
final class LiveCallController: ObservableObject {
    @Published private(set) var phase = "Live calling is unavailable: WebRTC is not linked in this build."
    @Published private(set) var running = false
    @Published var error: String?
    func start() async { error = "Live audio requires the WebRTC dependency and host live signaling support." }
    func stop() { running = false; phase = "Call ended." }
}
