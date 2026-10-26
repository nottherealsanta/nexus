import SwiftUI
import NexusKit

private struct SessionListItem: Identifiable {
    let id: String
    let value: JSONValue
}

/// Uses split-view selection on iPad and the same sidebar/detail stack on iPhone.
struct RootView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var code = ""
    @State private var search = ""
    @State private var showConnection = false
    @State private var confirmForget = false
    @State private var compactColumn: NavigationSplitViewColumn = .sidebar
    private var filteredSessions: [SessionListItem] {
        store.sessions.compactMap { row in
            guard let id = row["id"]?.string else { return nil }
            let title = row["title"]?.string ?? ""
            guard search.isEmpty || title.localizedCaseInsensitiveContains(search) || id.localizedCaseInsensitiveContains(search) else { return nil }
            return SessionListItem(id: id, value: row)
        }
    }
    var body: some View {
        Group {
            if store.isPaired {
                NavigationSplitView(preferredCompactColumn: $compactColumn) {
                    sessionList
                } detail: {
                    if store.selectedSession != nil { ConversationView() }
                    else { ContentUnavailableView("Your workspace, anywhere", systemImage: "bubble.left.and.bubble.right", description: Text("Choose a session or start a new conversation.")) }
                }
                .sheet(isPresented: $showConnection) {
                    NavigationStack {
                        connection.navigationTitle("Connection")
                            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showConnection = false } } }
                    }
                }
            } else { NavigationStack { connection.navigationTitle("Welcome to Nexus") } }
        }
        .task { if store.isPaired { await store.setForeground(true) } }
        .onChange(of: scenePhase) { _, phase in Task { await store.setForeground(phase == .active) } }
        .onChange(of: store.selectedSession) { _, id in if id != nil { compactColumn = .detail } }
        .confirmationDialog("Forget this device?", isPresented: $confirmForget, titleVisibility: .visible) {
            Button("Forget device and local drafts", role: .destructive) { store.forget(); showConnection = false }
        } message: { Text("You will need a new pairing code to reconnect. To revoke access on the server, use nexus gateway revoke.") }
    }
    private var sessionList: some View {
        List(selection: Binding(get: { store.selectedSession }, set: { id in
            if let id { Task { await store.selectSession(id) } }
        })) {
            Section {
                Label(store.connecting ? "Connecting…" : store.online ? "Connected" : "Connection interrupted", systemImage: store.online ? "checkmark.circle.fill" : "wifi.exclamationmark")
                    .foregroundStyle(store.online ? .green : .orange).font(.subheadline)
                Picker("Workspace", selection: Binding(get: { store.selectedHost ?? "" }, set: { id in Task { await store.selectHost(id) } })) {
                    Text("Choose workspace").tag("")
                    ForEach(store.hosts.indices, id: \.self) { index in
                        let host = store.hosts[index]
                        Text(host["label"]?.string ?? host["workspace"]?.string ?? host["host_id"]?.string ?? "Workspace").tag(host["host_id"]?.string ?? "")
                    }
                }.disabled(store.connecting)
                if !store.scopes.contains("prompt") { Label("Read-only device", systemImage: "lock").font(.caption).foregroundStyle(.secondary) }
            }
            Section {
                if store.loadingSessions { ProgressView("Loading sessions…") }
                else if store.hosts.isEmpty && !store.connecting {
                    ContentUnavailableView("No workspaces available", systemImage: "folder.badge.questionmark", description: Text("Allow a workspace on your Nexus machine and include it when pairing this device."))
                } else if store.selectedHost == nil { Text("Choose a workspace to see its sessions.").foregroundStyle(.secondary) }
                else if filteredSessions.isEmpty {
                    ContentUnavailableView(search.isEmpty ? "No sessions yet" : "No matching sessions", systemImage: "bubble.left.and.bubble.right", description: Text(search.isEmpty ? "Start a conversation using the compose button." : "Search covers sessions loaded from this workspace."))
                }
                ForEach(filteredSessions) { item in
                    NavigationLink(value: item.id) {
                        VStack(alignment: .leading, spacing: 7) {
                            Text((item.value["title"]?.string).flatMap { $0.isEmpty ? nil : $0 } ?? "Untitled session").font(.headline).lineLimit(2)
                            HStack {
                                Label(item.value["state"]?.string ?? "idle", systemImage: "bubble.left")
                                Spacer()
                                Text("\(item.value["message_count"]?.int ?? 0) messages")
                            }.font(.caption).foregroundStyle(.secondary)
                        }.padding(.vertical, 5)
                    }
                }
            } header: { Text("Active sessions") } footer: {
                if store.truncated { Text("Showing the first \(store.sessions.count) sessions. Search covers only these loaded sessions.") }
            }
            if let error = store.error {
                Section("Connection details") {
                    Text(error).foregroundStyle(.red)
                    Button("Try reconnecting") { Task { await store.reload() } }.disabled(store.connecting)
                }
            }
        }
        .searchable(text: $search, prompt: "Search loaded sessions")
        .navigationTitle("Nexus")
        .refreshable { await store.reload() }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { Button("Connection", systemImage: "network") { showConnection = true } }
            ToolbarItem(placement: .primaryAction) {
                Button("New session", systemImage: "square.and.pencil") { Task { await store.createSession() } }
                    .disabled(store.selectedHost == nil || !store.canPrompt || store.busy)
            }
        }
    }
    private var connection: some View {
        Form {
            Section {
                Label("Your Nexus, on your iPhone", systemImage: "network").font(.headline)
                Text("Connect Tailscale on both devices, then enter your gateway’s HTTPS address and a one-use pairing code.").foregroundStyle(.secondary)
            }
            Section("Gateway") {
                TextField("https://your-mac.tailnet.ts.net", text: $store.editableURL)
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    .accessibilityIdentifier("gatewayURL").disabled(store.isPaired || store.connecting)
                if !store.isPaired {
                    SecureField("Pairing code", text: $code).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityIdentifier("pairingCode").disabled(store.connecting)
                    Button { Task { await store.pair(code: code); if store.isPaired { code = "" } } } label: {
                        HStack { Text("Pair device"); if store.connecting { ProgressView() } }
                    }.disabled(store.connecting || code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || store.editableURL.isEmpty)
                } else {
                    Text("Access: \(store.scopes.joined(separator: ", "))").font(.footnote)
                    Button("Reconnect") { Task { await store.reload() } }.disabled(store.connecting)
                    Button("Forget this device", role: .destructive) { confirmForget = true }
                }
            }
            Section("On your Nexus machine") {
                Text("Allow your workspace, start the gateway behind Tailscale Serve, then run:")
                Text("nexus gateway pair --name phone").font(.system(.footnote, design: .monospaced)).textSelection(.enabled)
                Text("The default pairing code expires after two minutes. Credentials are stored in device-only Keychain.").font(.footnote).foregroundStyle(.secondary)
                Text("Keep the app open for live updates. Background notifications are not available.").font(.footnote).foregroundStyle(.secondary)
            }
            if let error = store.error { Section("Connection details") { Text(error).foregroundStyle(.red) } }
        }
    }
}
