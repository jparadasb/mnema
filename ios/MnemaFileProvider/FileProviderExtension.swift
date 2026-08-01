import FileProvider
import UniformTypeIdentifiers

final class FileProviderExtension: NSFileProviderReplicatedExtension {
    private let client = APIClient()

    required init(domain: NSFileProviderDomain) { super.init(domain: domain) }
    override func invalidate() {}

    private func remoteIdentifier(_ identifier: NSFileProviderItemIdentifier) -> String {
        identifier == .rootContainer ? "root" : identifier.rawValue
    }

    override func item(
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

    override func fetchContents(
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

    override func modifyItem(
        _ item: NSFileProviderItem,
        baseVersion version: NSFileProviderItemVersion,
        changedFields: NSFileProviderItemFields,
        contents newContents: URL?,
        options: NSFileProviderModifyItemOptions = [],
        request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        completionHandler(nil, changedFields, false, NSFileProviderError(.featureUnsupported))
        return progress
    }

    override func deleteItem(
        identifier: NSFileProviderItemIdentifier,
        baseVersion version: NSFileProviderItemVersion,
        options: NSFileProviderDeleteItemOptions = [],
        request: NSFileProviderRequest,
        completionHandler: @escaping (Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        completionHandler(NSFileProviderError(.featureUnsupported))
        return progress
    }

    override func createItem(
        basedOn itemTemplate: NSFileProviderItem,
        fields: NSFileProviderItemFields,
        contents url: URL?,
        options: NSFileProviderCreateItemOptions = [],
        request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        guard itemTemplate.parentItemIdentifier.rawValue == "inbox", let url else {
            completionHandler(nil, [], false, NSFileProviderError(.featureUnsupported))
            return progress
        }
        Task {
            do {
                let identifier = try await client.upload(
                    file: url,
                    name: itemTemplate.filename,
                    contentType: itemTemplate.typeIdentifier
                )
                let item = try await client.item(identifier)
                progress.completedUnitCount = 1
                completionHandler(FileProviderItem(item), [], false, nil)
            } catch { completionHandler(nil, [], false, error) }
        }
        return progress
    }

    override func enumerator(
        for containerItemIdentifier: NSFileProviderItemIdentifier,
        request: NSFileProviderRequest
    ) throws -> NSFileProviderEnumerator {
        FileProviderEnumerator(container: containerItemIdentifier, client: client)
    }
}
