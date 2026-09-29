"""文档来源回填：钉钉同步文档 source=dingtalk。

「添加知识」钉钉两条入库路径（单次导入 / 自动同步）此前未写 library_documents.source，
一律落默认值 local，来源列无法区分钉钉同步与本地上传。新代码已按路径打标；
本迁移仅回填可通过 sync_document_mappings 精确证明来源的历史数据（library 模式同步源的
dify_document_id 即 LibraryDocument UUID），一次性导入（无映射记录）无法回溯、保持原值。
"""
from alembic import op

revision = "0047_doc_dingtalk_source"
down_revision = "0046_sync_source_node_whitelist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE library_documents d
        SET source = 'dingtalk'
        WHERE d.source = 'local'
          AND d.id::text IN (
              SELECT m.dify_document_id
              FROM sync_document_mappings m
              JOIN sync_sources s ON s.id = m.source_id
              WHERE s.backend_type = 'library'
                AND m.dify_document_id IS NOT NULL
                AND m.dify_document_id <> ''
          )
    """)


def downgrade() -> None:
    # 回填不可逆：钉钉来源的精确判定依赖同步映射，降级会把真正同步来的文档降回 local
    pass
