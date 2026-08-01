"""Add guarded iCloud cleanup control plane.

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op

from mnema.jobs.models import (
    ICloudAsset,
    ICloudAssetComponent,
    ICloudCleanupEntry,
    ICloudCleanupManifest,
    ICloudQuotaObservation,
)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("archive_items")}
    if "cold_archived_at" not in columns:
        op.add_column("archive_items", sa.Column("cold_archived_at", sa.DateTime(timezone=True)))
    bind.execute(
        sa.text(
            "UPDATE archive_items SET cold_archived_at = ("
            "SELECT MAX(created_at) FROM audit_events "
            "WHERE audit_events.archive_item_id = archive_items.id "
            "AND audit_events.to_state = 'COLD_ARCHIVED')"
        )
    )
    for table in (
        ICloudAsset.__table__,
        ICloudQuotaObservation.__table__,
        ICloudCleanupManifest.__table__,
        ICloudAssetComponent.__table__,
        ICloudCleanupEntry.__table__,
    ):
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        ICloudCleanupEntry.__table__,
        ICloudAssetComponent.__table__,
        ICloudCleanupManifest.__table__,
        ICloudQuotaObservation.__table__,
        ICloudAsset.__table__,
    ):
        table.drop(bind, checkfirst=True)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("archive_items")}
    if "cold_archived_at" in columns:
        op.drop_column("archive_items", "cold_archived_at")
