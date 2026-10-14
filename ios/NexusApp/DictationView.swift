import SwiftUI
import NexusKit

struct DictationView: View {
    @EnvironmentObject var app: AppStore
    @StateObject private var recorder = DictationRecorder()
    @State private var transcript: String?
    @State private var busy = false
    @State private var status = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Dictation").font(.title2.bold())
            Text("Records up to 2 minutes and sends the audio to the selected host for transcription. Nothing is sent as a command until you review and submit it.").font(.footnote).foregroundStyle(.secondary)
            if recorder.recording { Text("Recording · \(recorder.remaining)s remaining").foregroundStyle(.red) }
            if let transcript { Text(transcript).textSelection(.enabled); Button("Insert into draft") { app.draft += (app.draft.isEmpty ? "" : "\n") + transcript; self.transcript = nil; recorder.discard() } }
            HStack {
                Button(recorder.recording ? "Stop recording" : "Record") { if recorder.recording { recorder.stop() } else { Task { await recorder.start() } } }.disabled(busy)
                Button("Transcribe") { Task { await transcribe() } }.disabled(recorder.recording || recorder.fileURL == nil || busy)
                Button("Cancel") { recorder.discard(); transcript = nil }.disabled(recorder.fileURL == nil && transcript == nil)
            }
            if let error = recorder.error { Text(error).foregroundStyle(.red) }
            if !status.isEmpty { Text(status).font(.footnote) }
        }.padding().onDisappear { recorder.discard() }
    }
    private func transcribe() async {
        guard let client = app.client, let host = app.selectedHost, let url = recorder.fileURL,
              let data = try? Data(contentsOf: url), data.count <= 8 * 1024 * 1024 else { status = "Select a host and record a short audio clip."; return }
        busy = true; defer { busy = false }
        do {
            let value = try await client.voice(hostID: host, requestID: UUID(), wav: data)
            guard case let .object(object) = value, case let .string(text)? = object["text"] else { status = "Host returned no transcription text."; return }
            transcript = text; status = "Review the transcription, then choose Insert into draft."
        } catch { status = "Transcription failed: \(error.localizedDescription)" }
    }
}

