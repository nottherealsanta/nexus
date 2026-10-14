import Foundation
import Security

struct PairingCredential: Codable {
    let token: String
    let scopes: [String]
    let deviceID: String
    let boundURL: String
}

enum Keychain {
    private static let service = "com.nexus.ios.device"
    static func read() -> PairingCredential? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
            kSecReturnData as String: true, kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(PairingCredential.self, from: data)
    }
    static func save(_ credential: PairingCredential) throws {
        remove()
        let data = try JSONEncoder().encode(credential)
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
            kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        guard SecItemAdd(query as CFDictionary, nil) == errSecSuccess else { throw AppFailure("Could not securely save pairing credentials") }
    }
    static func remove() { SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service] as CFDictionary) }
}
