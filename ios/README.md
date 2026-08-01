# Mnema for iPhone

Private iOS 17+ SwiftUI app and replicated File Provider extension. Generate the Xcode
project with `xcodegen generate`, set the Apple development team, enable App Groups and
File Provider for both identifiers, then build the `Mnema` scheme.

Both targets must include the `group.com.jparadasb.mnema` App Group in Signing &
Capabilities. Mnema uses that App Group as its shared Keychain access group, so the host
app and File Provider extension can access the same device credentials.

Pairing uses `mnema file-provider pair`. Credentials live in the shared Keychain group.
The extension exposes read-only Archive collections and permits file creation only in Inbox.
