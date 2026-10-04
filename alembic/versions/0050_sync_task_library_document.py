"""SyncTask.library_document_id: library 批处理模式的任务→预建文档关联。"""
from alembic import op
import sqlalchemy as sa

revision = "0050_sync_task_library_document"
down_revision = "0049_material_libraries"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sync_tasks",
                  sa.Column("library_document_id", sa.String(36), nullable=True))
    op.create_index("ix_sync_tasks_library_document_id", "sync_tasks", ["library_document_id"])


def downgrade():
    op.drop_index("ix_sync_tasks_library_document_id", table_name="sync_tasks")
    op.drop_column("sync_tasks", "library_document_id")
