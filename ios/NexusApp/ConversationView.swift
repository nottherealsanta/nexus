import SwiftUI
import NexusKit

private struct ConversationItem: Identifiable {
    let id: String
    let value: JSONValue
    static func rows(_ values: [JSONValue]) -> [ConversationItem] {
        values.enumerated().map { ConversationItem(id: $0.element["id"]?.string ?? $0.element["call_id"]?.string ?? String($0.offset), value: $0.element) }
    }
}

struct ConversationView: View {
    @EnvironmentObject private var store: AppStore
    @State private var followLatest = true
    @FocusState private var composing: Bool
    var body: some View {
        VStack(spacing: 0) {
            connectionStatus
            ScrollViewReader { reader in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if let view = store.conversation {
                            if (view["turns"]?.array ?? []).isEmpty {
                                ContentUnavailableView("Start a conversation", systemImage: "bubble.left.and.text.bubble.right", description: Text("Messages and tools will appear here as Nexus works."))
                            }
                            ForEach(ConversationItem.rows(view["turns"]?.array ?? [])) { turn in TurnCard(turn: turn.value) }
                            if !(view["input_queue"]?.array ?? []).isEmpty {
                                DisclosureGroup("Queued messages (\(view["input_queue"]?.array?.count ?? 0))") { StructuredValueView(value: view["input_queue"] ?? .null) }
                            }
                            DisclosureGroup("Session context and all details") { StructuredValueView(value: view) }
                        } else if store.loading { ProgressView("Opening session…").frame(maxWidth: .infinity).padding(.top, 40) }
                        else {
                            ContentUnavailableView("Unable to load this session", systemImage: "wifi.exclamationmark", description: Text("Reconnect to your gateway to try again."))
                            Button("Retry") { Task { await store.refreshConversation() } }
                        }
                        Color.clear.frame(height: 1).id("latest")
                    }.padding()
                }
                .scrollDismissesKeyboard(.interactively)
                .refreshable { await store.refreshConversation() }
                .onChange(of: store.snapshot?["seq"]?.int) { _, _ in if followLatest { reader.scrollTo("latest", anchor: .bottom) } }
                .onChange(of: store.selectedSession) { _, _ in followLatest = true }
            }
            if !store.pendingPermissions.isEmpty { approvalRequests }
            Divider()
            composer
        }
        .navigationTitle(store.sessionTitle).navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button(followLatest ? "Pause following" : "Follow latest", systemImage: followLatest ? "pause.circle" : "arrow.down.circle") { followLatest.toggle() }
                    .accessibilityHint("Pause following to read earlier messages without scrolling")
                if store.conversation?["phase"]?.string == "running" {
                    Button("Stop", systemImage: "stop.circle") { Task { await store.stopTurn() } }
                        .disabled(store.busy || !store.canPrompt)
                }
                Button("Refresh", systemImage: "arrow.clockwise") { Task { await store.reload() } }.disabled(store.connecting || store.loading)
            }
        }
    }
    @ViewBuilder private var connectionStatus: some View {
        if !store.online {
            VStack(alignment: .leading, spacing: 4) {
                Label("Connection interrupted · showing saved content", systemImage: "wifi.exclamationmark").font(.caption.bold())
                if let date = store.lastUpdated { Text("Last updated \(date.formatted(date: .omitted, time: .shortened))").font(.caption) }
            }.foregroundStyle(.orange).frame(maxWidth: .infinity, alignment: .leading).padding()
        }
        if let error = store.error { Text(error).font(.caption).foregroundStyle(.red).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal) }
    }
    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !store.scopes.contains("prompt") { Label("This device has read-only access", systemImage: "lock").font(.caption).foregroundStyle(.secondary) }
            HStack(alignment: .bottom, spacing: 12) {
                TextField("Message Nexus", text: Binding(get: { store.draft }, set: store.updateDraft), axis: .vertical)
                    .lineLimit(1...6).focused($composing).accessibilityLabel("Message draft")
                    .padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                Button { followLatest = true; Task { await store.send() } } label: {
                    if store.busy { ProgressView().frame(width: 30, height: 30) }
                    else { Image(systemName: "arrow.up.circle.fill").font(.system(size: 32)) }
                }.accessibilityLabel("Send message").keyboardShortcut(.return, modifiers: .command)
                    .disabled(store.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !store.canPrompt || store.busy || store.loading)
            }
            if !store.online { Text("You can keep writing. Your draft is saved on this device.").font(.caption).foregroundStyle(.secondary) }
        }.padding()
    }
    private var approvalRequests: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(ConversationItem.rows(store.pendingPermissions)) { item in
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Approval needed · \(item.value["tool"]?.string ?? "Tool")", systemImage: "hand.raised").font(.headline)
                        if let preview = item.value["preview"]?.string { Text(preview).font(.system(.footnote, design: .monospaced)).textSelection(.enabled) }
                        DisclosureGroup("Full request details") { StructuredValueView(value: item.value) }
                        if store.scopes.contains("approve") {
                            HStack {
                                Button("Allow once") { Task { await store.resolvePermission(item.id, decision: "allow_once") } }.buttonStyle(.borderedProminent)
                                Button("Deny", role: .destructive) { Task { await store.resolvePermission(item.id, decision: "deny_once") } }.buttonStyle(.bordered)
                            }.disabled(!store.online || store.busy)
                        } else { Text("Answer this request from a device with approval access.").font(.caption).foregroundStyle(.secondary) }
                    }
                }
            }.padding()
        }.frame(maxHeight: 240).background(.thinMaterial)
    }
}

private struct TimelineEntry: Identifiable {
    let id: String
    let value: JSONValue
    let tool: Bool
    let order: Int
}

private struct TurnCard: View {
    let turn: JSONValue
    private var entries: [TimelineEntry] {
        let messages = ConversationItem.rows(turn["messages"]?.array ?? []).enumerated().map {
            TimelineEntry(id: "message:" + $0.element.id, value: $0.element.value, tool: false, order: $0.offset)
        }
        let tools = ConversationItem.rows(turn["tools"]?.array ?? []).enumerated().map {
            TimelineEntry(id: "tool:" + $0.element.id, value: $0.element.value, tool: true, order: messages.count + $0.offset)
        }
        return (messages + tools).sorted {
            let left = $0.value["event_seq"]?.int ?? 0; let right = $1.value["event_seq"]?.int ?? 0
            return left == right ? $0.order < $1.order : left < right
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(entries) { entry in
                if entry.tool { ToolCard(tool: entry.value) }
                else { MessageCard(message: entry.value) }
            }
            if let error = turn["error"]?.string, !error.isEmpty { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
            DisclosureGroup("Turn context and all details") { StructuredValueView(value: turn) }.font(.caption)
        }
    }
}

private struct ToolCard: View {
    let tool: JSONValue
    var body: some View {
        DisclosureGroup {
            StructuredValueView(value: tool)
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                Label(tool["name"]?.string ?? "Tool call", systemImage: "terminal").font(.subheadline.bold())
                Text(tool["status"]?.string ?? "requested").font(.caption).foregroundStyle(.secondary)
                if let target = tool["target"]?.string { Text(target).font(.system(.caption, design: .monospaced)).lineLimit(2) }
            }
        }.padding().background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
    }
}

private struct MessageCard: View {
    let message: JSONValue
    private var fromUser: Bool { message["role"]?.string == "user" }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(fromUser ? "You" : (message["role"]?.string ?? "Nexus").capitalized,
                  systemImage: fromUser ? "person.crop.circle" : "sparkle")
                .font(.caption.bold()).foregroundStyle(.secondary).accessibilityAddTraits(.isHeader)
            ForEach(Array((message["blocks"]?.array ?? []).enumerated()), id: \.offset) { _, block in
                if block["kind"]?.string == "text", let text = block["text"]?.string {
                    Text(text).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                } else if block["kind"]?.string == "thinking" {
                    DisclosureGroup("Reasoning") { StructuredValueView(value: block) }
                } else { StructuredValueView(value: block) }
            }
            DisclosureGroup("Message details") { StructuredValueView(value: message) }.font(.caption)
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            .background(fromUser ? Color.accentColor.opacity(0.09) : Color.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 16))
    }
}
