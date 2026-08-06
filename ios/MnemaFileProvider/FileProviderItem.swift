import FileProvider
import UniformTypeIdentifiers

final class FileProviderItem: NSObject, NSFileProviderItem, NSFileProviderItemDecorating {
    private static let mnemaDecoration = NSFileProviderItemDecorationIdentifier(
        "com.jparadasb.mnema.fileprovider.badge"
    )
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
    var contentType: UTType {
        remote.kind == "folder" ? .folder : UTType(remote.contentType) ?? .data
    }
    var itemVersion: NSFileProviderItemVersion {
        let contentVersion = remote.contentVersion.isEmpty
            ? "metadata:\(remote.metadataVersion)"
            : remote.contentVersion
        return NSFileProviderItemVersion(
            contentVersion: Data(contentVersion.utf8),
            metadataVersion: Data(remote.metadataVersion.utf8)
        )
    }
    var capabilities: NSFileProviderItemCapabilities {
        var result: NSFileProviderItemCapabilities = [.allowsReading]
        if remote.kind == "folder" { result.insert(.allowsContentEnumerating) }
        if remote.capabilities.contains("addFile") { result.insert(.allowsAddingSubItems) }
        if remote.capabilities.contains("delete") { result.insert(.allowsDeleting) }
        return result
    }

    var decorations: [NSFileProviderItemDecorationIdentifier]? {
        [Self.mnemaDecoration]
    }
}
