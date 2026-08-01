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
                    observer.finishEnumerating(upTo: nil)
                    return
                }
                let identifier = container == .rootContainer ? "root" : container.rawValue
                let response = try await client.children(identifier, offset: offset)
                guard !invalidated else { return }
                observer.didEnumerate(response.items.map(FileProviderItem.init))
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
        let cursor = String(data: syncAnchor.rawValue, encoding: .utf8)
        Task {
            do {
                let response = try await client.changes(cursor: cursor?.isEmpty == true ? nil : cursor)
                if response.resetRequired {
                    observer.finishEnumeratingWithError(NSFileProviderError(.syncAnchorExpired))
                    return
                }
                observer.didUpdate(response.changes.compactMap(\.item).map(FileProviderItem.init))
                let deleted = response.changes.filter { $0.operation == "delete" }.map {
                    NSFileProviderItemIdentifier($0.itemId)
                }
                if !deleted.isEmpty { observer.didDeleteItems(withIdentifiers: deleted) }
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
        Task {
            let response = try? await client.changes(cursor: nil)
            completionHandler(response.map { NSFileProviderSyncAnchor(Data($0.nextCursor.utf8)) })
        }
    }
}
