// CryptoKit verifies Sparkle's pure Ed25519 signature without copying a full archive.
import Foundation
import CryptoKit

@main struct VerifyUpdate {
    static func main() {
        do {
            guard CommandLine.arguments.count == 4,
                  let publicBytes = Data(base64Encoded: CommandLine.arguments[1]),
                  let signature = Data(base64Encoded: CommandLine.arguments[2]) else { exit(2) }
            let key = try Curve25519.Signing.PublicKey(rawRepresentation: publicBytes)
            let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[3]), options: .mappedIfSafe)
            exit(key.isValidSignature(signature, for: data) ? 0 : 1)
        } catch { exit(2) }
    }
}
