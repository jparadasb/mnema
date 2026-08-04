# Mnema for iPhone

Private iOS 17+ SwiftUI app and replicated File Provider extension. Generate the Xcode
project with `xcodegen generate`, set the Apple development team, enable App Groups and
File Provider for both identifiers, then build the `Mnema` scheme.

On macOS 12, install the same pinned XcodeGen release used by CI without Homebrew or
MacPorts:

```bash
./scripts/install-xcodegen-macos.sh
export PATH="$HOME/.local/bin:$PATH"
cd ios
xcodegen generate
open Mnema.xcodeproj
```

Both targets must include the `group.com.jparadasb.mnema` App Group in Signing &
Capabilities. Mnema uses that App Group as its shared Keychain access group, so the host
app and File Provider extension can access the same device credentials.

Pairing uses `mnema file-provider pair`. Credentials live in the shared Keychain group.
The extension exposes read-only Archive collections and permits file creation only in Inbox.

## Simulator E2E

The opt-in pytest E2E test starts a disposable HTTPS Mnema API, creates and trusts a
one-day test certificate in a temporary iPhone 14 simulator, and drives Mnema and Files
through Appium-managed WebDriverAgent. Build both simulator schemes without disabling
code signing, then run:

```bash
npm ci
npx appium driver install --source=local node_modules/appium-xcuitest-driver
MNEMA_E2E_IOS_APP=/path/to/Mnema.app \
MNEMA_E2E_IOS_FIXTURE_APP=/path/to/MnemaE2EFixture.app \
pytest tests/e2e/test_ios_file_provider.py --no-cov
```

The test skips outside its configured macOS environment. CI sets
`MNEMA_IOS_E2E_REQUIRED=1`, which turns missing prerequisites into failures.
