import SwiftUI

@main
struct NexusApp: App {
    @StateObject private var store: AppStore
    init() {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--ui-testing") {
            // UI tests must never read or delete a developer's device credential.
            _store = StateObject(wrappedValue: AppStore(defaults: UserDefaults(suiteName: "nexus.ui-tests")!, credential: nil,
                                                        saveCredential: { _ in }, removeCredential: {}))
        } else { _store = StateObject(wrappedValue: AppStore()) }
        #else
        _store = StateObject(wrappedValue: AppStore())
        #endif
    }
    var body: some Scene { WindowGroup { RootView().environmentObject(store) } }
}
