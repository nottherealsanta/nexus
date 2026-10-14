import SwiftUI

@main
struct NexusApp: App {
    @StateObject private var store = AppStore()
    var body: some Scene { WindowGroup { RootView().environmentObject(store) } }
}
