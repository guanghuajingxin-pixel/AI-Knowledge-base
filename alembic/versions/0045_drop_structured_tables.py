"""结构化处理功能下线：删除 structured_tasks / structured_schemas / structured_write_logs 三张表

Revision ID: 0045_drop_structured_tables
Revises: 0044_dingtalk_file_reviews
Create Date: 2026-09-28
"""
from alembic import op

revision = "0045_drop_structured_tables"
down_revision = "0044_dingtalk_file_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先删带外键的写入历史，再删被引用的两张表
    op.execute('DROP TABLE IF EXISTS structured_write_logs CASCADE')
    op.execute('DROP TABLE IF EXISTS structured_schemas CASCADE')
    op.execute('DROP TABLE IF EXISTS structured_tasks CASCADE')
    # 功能配置项一并清理
    op.execute("DELETE FROM settings WHERE key = 'structured_db_url'")


def downgrade() -> None:
    # 功能已下线，不做恢复；如需回滚请还原对应版本的代码后重建表
    pass
