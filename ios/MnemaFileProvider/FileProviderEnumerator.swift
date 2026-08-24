import FileProvider

final class FileProviderEnumerator: NSObject, NSFileProviderEnumerator {
    private let container: NSFileProviderItemIdentifier
    private let client: APIClient
    private var invalidated = false

    init(container: NSFileProviderItemIdentifier, client: APIClient) {
        self.container = container
        self.client = client
    }

    func invalidate() { invalidated = true }

    func enumerateItems(
        for observer: NSFileProviderEnumerationObserver,
        startingAt page: NSFileProviderPage
    ) {
        let offset = Int(String(data: page.rawValue, encoding: .utf8) ?? "0") ?? 0
        Task {
            do {
                if container == .workingSet {
                    let response = try await client.changes(cursor: nil)
                    guard !invalidated else { return }
                    var latest: [String: RemoteItem] = [:]
                    for change in response.changes {
                        if let item = change.item { latest[item.id] = item }
                    }
                    let items = latest.values.sorted { $0.id < $1.id }.map(FileProviderItem.init)
                    observer.didEnumerate(items)
                    // These items are now reported, so the anchor may advance to
                    // the cursor they were read at — and only to that cursor.
                    SharedSyncState.storeSyncAnchor(response.nextCursor)
                    SharedSyncState.markSuccessfulSync()
                    observer.finishEnumerating(upTo: nil)
                    return
                }
                let identifier = container == .rootContainer ? "root" : container.rawValue
                let response = try await client.children(identifier, offset: offset)
                guard !invalidated else { return }
                observer.didEnumerate(response.items.map(FileProviderItem.init))
                SharedSyncState.markSuccessfulSync()
                let next = response.nextOffset.map { NSFileProviderPage(Data(String($0).utf8)) }
                observer.finishEnumerating(upTo: next)
            } catch {
                observer.finishEnumeratingWithError(error)
            }
        }
    }

    func enumerateChanges(
        for observer: NSFileProviderChangeObserver,
        from syncAnchor: NSFileProviderSyncAnchor
    ) {
        let anchor = String(data: syncAnchor.rawValue, encoding: .utf8)
        let cursor = (anchor?.isEmpty ?? true) ? nil : anchor
        Task {
            do {
                let response = try await client.changes(cursor: cursor)
                if response.resetRequired {
                    SharedSyncState.clearSyncAnchor()
                    observer.finishEnumeratingWithError(NSFileProviderError(.syncAnchorExpired))
                    return
                }
                guard !invalidated else { return }
                // Deletions are applied before updates so that a delete followed
                // by a re-create inside one page settles on the created item.
                let deleted = response.changes.filter { $0.operation == "delete" }.map {
                    NSFileProviderItemIdentifier($0.itemId)
                }.filter { $0 != .rootContainer }
                if !deleted.isEmpty { observer.didDeleteItems(withIdentifiers: deleted) }
                let updated = response.changes
                    .filter { $0.operation != "delete" }
                    .compactMap(\.item)
                if !updated.isEmpty { observer.didUpdate(updated.map(FileProviderItem.init)) }
                // Only advance past changes that were actually reported. The
                // previous code fast-forwarded the anchor on an empty cursor
                // without emitting anything, permanently skipping those changes.
                SharedSyncState.storeSyncAnchor(response.nextCursor)
                SharedSyncState.markSuccessfulSync()
                observer.finishEnumeratingChanges(
                    upTo: NSFileProviderSyncAnchor(Data(response.nextCursor.utf8)),
                    moreComing: response.more
                )
            } catch {
                observer.finishEnumeratingWithError(error)
            }
        }
    }

    func currentSyncAnchor(completionHandler: @escaping (NSFileProviderSyncAnchor?) -> Void) {
        // Report the anchor this extension has actually reported items up to.
        // Returning the server's newest cursor claimed we were already current
        // and silently dropped every pending change; returning nil makes the
        // system perform a full re-enumeration, which is the safe fallback.
        guard let stored = SharedSyncState.syncAnchor, !stored.isEmpty else {
            completionHandler(nil)
            return
        }
        completionHandler(NSFileProviderSyncAnchor(Data(stored.utf8)))
    }
}
