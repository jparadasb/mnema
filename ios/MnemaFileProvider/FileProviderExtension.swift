import FileProvider
import UniformTypeIdentifiers

final class FileProviderExtension: NSObject, NSFileProviderReplicatedExtension,
    NSFileProviderEnumerating
{
    private let client = APIClient()

    required init(domain: NSFileProviderDomain) { super.init() }
    func invalidate() {}

    private func remoteIdentifier(_ identifier: NSFileProviderItemIdentifier) -> String {
        identifier == .rootContainer ? "root" : identifier.rawValue
    }

    func item(
        for identifier: NSFileProviderItemIdentifier,
        request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        Task {
            do {
                let item = try await client.item(remoteIdentifier(identifier))
                progress.completedUnitCount = 1
                completionHandler(FileProviderItem(item), nil)
            } catch { completionHandler(nil, error) }
        }
        return progress
    }

    func fetchContents(
        for itemIdentifier: NSFileProviderItemIdentifier,
        version requestedVersion: NSFileProviderItemVersion?,
        request: NSFileProviderRequest,
        completionHandler: @escaping (URL?, NSFileProviderItem?, Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        Task {
            do {
                let identifier = remoteIdentifier(itemIdentifier)
                async let remote = client.item(identifier)
                async let content = client.download(identifier)
                let result = try await (remote, content)
                progress.completedUnitCount = 1
                completionHandler(result.1, FileProviderItem(result.0), nil)
            } catch { completionHandler(nil, nil, error) }
        }
        return progress
    }

    func modifyItem(
        _ item: NSFileProviderItem,
        baseVersion version: NSFileProviderItemVersion,
        changedFields: NSFileProviderItemFields,
        contents newContents: URL?,
        options: NSFileProviderModifyItemOptions = [],
        request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        completionHandler(nil, changedFields, false, CocoaError(.featureUnsupported))
        return progress
    }

    func deleteItem(
        identifier: NSFileProviderItemIdentifier,
        baseVersion version: NSFileProviderItemVersion,
        options: NSFileProviderDeleteItemOptions = [],
        request: NSFileProviderRequest,
        completionHandler: @escaping (Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        completionHandler(CocoaError(.featureUnsupported))
        return progress
    }

    func createItem(
        basedOn itemTemplate: NSFileProviderItem,
        fields: NSFileProviderItemFields,
        contents url: URL?,
        options: NSFileProviderCreateItemOptions = [],
        request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        guard itemTemplate.parentItemIdentifier.rawValue == "inbox", let url else {
            completionHandler(nil, [], false, CocoaError(.featureUnsupported))
            return progress
        }
        Task {
            do {
                let identifier = try await client.upload(
                    file: url,
                    name: itemTemplate.filename,
                    contentType: itemTemplate.typeIdentifier ?? UTType.data.identifier
                )
                let item = try await client.item(identifier)
                progress.completedUnitCount = 1
                completionHandler(FileProviderItem(item), [], false, nil)
            } catch { completionHandler(nil, [], false, error) }
        }
        return progress
    }

    func enumerator(
        for containerItemIdentifier: NSFileProviderItemIdentifier,
        request: NSFileProviderRequest
    ) throws -> NSFileProviderEnumerator {
        FileProviderEnumerator(container: containerItemIdentifier, client: client)
    }
}
