import FileProvider
import UniformTypeIdentifiers

final class FileProviderItem: NSObject, NSFileProviderItem {
    let remote: RemoteItem
    init(_ remote: RemoteItem) { self.remote = remote }

    var itemIdentifier: NSFileProviderItemIdentifier {
        remote.id == "root" ? .rootContainer : .init(remote.id)
    }
    var parentItemIdentifier: NSFileProviderItemIdentifier {
        guard let parent = remote.parentId, parent != "root" else { return .rootContainer }
        return NSFileProviderItemIdentifier(parent)
    }
    var filename: String { remote.name }
    var typeIdentifier: String {
        remote.kind == "folder" ? UTType.folder.identifier : remote.contentType
    }
    var documentSize: NSNumber? { NSNumber(value: remote.size) }
    var contentModificationDate: Date? { remote.modifiedAt }
    var itemVersion: NSFileProviderItemVersion {
        NSFileProviderItemVersion(
            contentVersion: Data(remote.contentVersion.utf8),
            metadataVersion: Data(remote.metadataVersion.utf8)
        )
    }
    var capabilities: NSFileProviderItemCapabilities {
        var result: NSFileProviderItemCapabilities = [.allowsReading]
        if remote.kind == "folder" { result.insert(.allowsContentEnumerating) }
        if remote.capabilities.contains("addFile") { result.insert(.allowsAddingSubItems) }
        return result
    }
}
