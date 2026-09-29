"""同步源节点白名单：sync_sources.node_whitelist。

「添加知识」选择指定钉钉文档 + 自动同步时，把选中的节点 ID 写入白名单，
同步引擎只处理白名单内的节点；空数组表示同步整棵目录树（历史行为不变）。
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_sync_source_node_whitelist"
down_revision = "0045_drop_structured_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sync_sources", sa.Column(
        "node_whitelist", sa.Text(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("sync_sources", "node_whitelist")
