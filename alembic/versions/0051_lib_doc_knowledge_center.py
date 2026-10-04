"""LibraryDocument 知识中心字段：钉钉来源固化信息 + 更新人 + 过期时间。"""
from alembic import op
import sqlalchemy as sa

revision = "0051_lib_doc_knowledge_center"
down_revision = "0050_sync_task_library_document"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("library_documents", sa.Column("source_url", sa.String(500), nullable=True))
    op.add_column("library_documents", sa.Column("source_workspace_name", sa.String(200), nullable=True))
    op.add_column("library_documents", sa.Column("updated_by", sa.String(100), nullable=True))
    op.add_column("library_documents", sa.Column("expire_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("library_documents", "expire_at")
    op.drop_column("library_documents", "updated_by")
    op.drop_column("library_documents", "source_workspace_name")
    op.drop_column("library_documents", "source_url")
