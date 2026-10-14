import AVFoundation
import Foundation

@MainActor
final class DictationRecorder: NSObject, ObservableObject, AVAudioRecorderDelegate {
    @Published private(set) var recording = false
    @Published private(set) var remaining = 120
    @Published private(set) var fileURL: URL?
    @Published var error: String?
    private var recorder: AVAudioRecorder?
    private var timer: Timer?
    private let limitBytes = 8 * 1024 * 1024

    func start() async {
        guard !recording else { return }
        let session = AVAudioSession.sharedInstance()
        do {
            let granted = await withCheckedContinuation { continuation in
                session.requestRecordPermission { continuation.resume(returning: $0) }
            }
            guard granted else { error = "Microphone access is off. Enable it in Settings to record a dictation."; return }
            try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
            try session.setActive(true)
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("nexus-dictation-\(UUID().uuidString).wav")
            let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16_000, AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsFloatKey: false]
            let r = try AVAudioRecorder(url: url, settings: settings)
            r.delegate = self; r.record(forDuration: 120)
            recorder = r; fileURL = url; recording = true; remaining = 120
            timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                Task { @MainActor in
                    guard let self, let r = self.recorder else { return }
                    self.remaining = max(0, 120 - Int(r.currentTime))
                    if let attrs = try? FileManager.default.attributesOfItem(atPath: r.url.path), (attrs[.size] as? NSNumber)?.intValue ?? 0 > self.limitBytes { self.stop() }
                }
            }
        } catch { self.error = "Could not start microphone recording: \(error.localizedDescription)"; discard() }
    }
    func stop() { guard let recorder else { return }; recorder.stop(); recording = false; timer?.invalidate(); timer = nil }
    func discard() { stop(); if let fileURL { try? FileManager.default.removeItem(at: fileURL) }; fileURL = nil; recorder = nil }
    func consume() -> URL? { defer { fileURL = nil; recorder = nil }; return fileURL }
    func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) { recording = false; timer?.invalidate(); timer = nil; if !flag { error = "Recording did not finish. Try again." } }
    deinit { timer?.invalidate(); if let fileURL { try? FileManager.default.removeItem(at: fileURL) } }
}
