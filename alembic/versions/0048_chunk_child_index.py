"""子分段稳定排序：library_chunks.child_index。

父子分段（Dify parent-child 移植）子分段在前端分段页随父分段展示，需要父块内
稳定顺序。created_at 为事务时间戳（同一解析事务内完全相同）、id 为随机 UUID，
均无法稳定排序，故新增 child_index（父块内序号，父分段为 NULL）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0048_chunk_child_index"
down_revision = "0047_doc_dingtalk_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("library_chunks", sa.Column("child_index", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("library_chunks", "child_index")
