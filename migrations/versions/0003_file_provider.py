"""Add File Provider metadata, change journal, uploads, and device credentials.

Revision ID: 0003
"""

from alembic import op

from mnema.jobs.models import (
    FileProviderChange,
    FileProviderDevice,
    FileProviderItem,
    FileProviderPairingCode,
    FileProviderUpload,
)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in (
        FileProviderItem.__table__,
        FileProviderChange.__table__,
        FileProviderPairingCode.__table__,
        FileProviderDevice.__table__,
        FileProviderUpload.__table__,
    ):
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        FileProviderUpload.__table__,
        FileProviderDevice.__table__,
        FileProviderPairingCode.__table__,
        FileProviderChange.__table__,
        FileProviderItem.__table__,
    ):
        table.drop(bind, checkfirst=True)
