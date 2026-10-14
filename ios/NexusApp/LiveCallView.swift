import SwiftUI

struct LiveCallView: View {
    @EnvironmentObject var app: AppStore
    @StateObject private var call = LiveCallController()
    @State private var consent = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Live audio").font(.title2.bold())
            Text("Live audio sends microphone audio directly to the configured voice provider through the host. No provider credentials are needed on this device.").font(.footnote).foregroundStyle(.secondary)
            Text(call.phase)
            if let error = call.error { Text(error).foregroundStyle(.red) }
            if consent {
                Button("I agree — start live audio") { Task { await call.start() } }
            } else {
                Button("Review and continue") { consent = true }
            }
            Button("End call") { call.stop() }
            Text("Tool approvals must be reviewed and tapped in the conversation; do not treat audio as approval.").font(.caption).foregroundStyle(.secondary)
        }.padding().onDisappear { call.stop() }
    }
}
