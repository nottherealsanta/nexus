import SwiftUI
import NexusKit

struct ConversationView: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        VStack(spacing: 0) {
            if !store.online { Label("Offline or showing stale content", systemImage: "exclamationmark.triangle").foregroundStyle(.orange).padding() }
            ScrollView {
                if let snapshot = store.snapshot { StructuredValueView(value: snapshot).padding() }
                else { ContentUnavailableView("Choose a session", systemImage: "bubble.left.and.bubble.right") }
            }
            Divider()
            HStack(alignment: .bottom) {
                TextField("Message", text: Binding(get: { store.draft }, set: store.updateDraft), axis: .vertical).lineLimit(1...6).accessibilityLabel("Message draft")
                Button("Send", systemImage: "arrow.up.circle.fill") { Task { await store.send() } }.disabled(store.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !store.online)
            }.padding()
        }.navigationTitle("Conversation").toolbar {
            Button("Refresh", systemImage: "arrow.clockwise") { Task { await store.reload() } }
        }
    }
}
