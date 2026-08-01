# Mnema for iPhone

Private iOS 17+ SwiftUI app and replicated File Provider extension. Generate the Xcode
project with `xcodegen generate`, set the Apple development team, enable App Groups and
File Provider for both identifiers, then build the `Mnema` scheme.

Pairing uses `mnema file-provider pair`. Credentials live in the shared Keychain group.
The extension exposes read-only Archive collections and permits file creation only in Inbox.
