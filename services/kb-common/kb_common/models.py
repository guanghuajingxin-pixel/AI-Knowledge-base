import uuid
from datetime import datetime, date
from sqlalchemy import String, Text, Integer, BigInteger, Boolean, DateTime, Date, JSON, ForeignKey, Float, func, UniqueConstraint, text as sa_text
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # super_admin|admin|editor|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # 钉钉建档/管理员重置密码后为 true：下次登录强制跳设密页（迁移 0041）
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"), nullable=False)
    # 最近一次「用户自主设置/修改」密码的时间；null 表示从未设过可用密码
    password_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    kb_type: Mapped[str] = mapped_column(String(20))  # DOCUMENT | FAQ
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    chunk_strategy: Mapped[str] = mapped_column(String(30), default="FIXED_SIZE")
    chunk_size: Mapped[int] = mapped_column(Integer, default=512)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=150)
    delimiter: Mapped[str | None] = mapped_column(String(50))  # Delimiter 策略分隔符
    embedding_model: Mapped[str] = mapped_column(String(100), default="bge-m3")
    es_index_name: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="FULLY_PUBLISHED")  # PUBLISHING|FULLY_PUBLISHED|PARTIALLY_FAILED
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KbFavorite(Base):
    __tablename__ = "kb_favorites"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Directory(Base):
    __tablename__ = "directories"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("directories.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    knowledge_owner: Mapped[str] = mapped_column(String(200), default="", server_default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    directory_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("directories.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(20))
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    storage_path: Mapped[str] = mapped_column(String(1000))   # MinIO raw key
    parsed_path: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    # PENDING->PARSING->CHUNKING->EMBEDDING->INDEXING->COMPLETED|FAILED
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    uploader_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

class Segment(Base):
    __tablename__ = "segments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    faq_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("faq_entries.id", ondelete="CASCADE"))
    es_chunk_id: Mapped[str | None] = mapped_column(String(100))
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    page_number: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

class FaqDirectory(Base):
    __tablename__ = "faq_directories"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("faq_directories.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

class FaqEntry(Base):
    __tablename__ = "faq_entries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    directory_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("faq_directories.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(ARRAY(String), default=list)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT|INDEXED|FAILED
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

class Setting(Base):
    """运行时可配置项（LLM/MinerU key 等），覆盖 .env 默认值。"""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """敏感操作审计：管理员重置密码/解绑钉钉/角色变更、用户自主改密等（迁移 0042）。"""
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32))          # user / setting / kb
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    detail: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DifyProfile(Base):
    """多条 Dify 配置，只能生效一条（enabled=true）。"""
    __tablename__ = "dify_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    dataset_ids: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LLMProfile(Base):
    """LLM 供应商配置：一个供应商一条；models 为 JSON 数组。

    models 结构：[{"name": "deepseek-chat", "enabled": true, "is_default": false}, ...]
    - enabled=true 的模型出现在智能问答模型选项中
    - 全局仅一个模型 is_default=true，作为新会话默认模型
    """
    __tablename__ = "llm_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(32), default="custom")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    models: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RerankProfile(Base):
    """多条 Rerank 重排模型配置，只能生效一条（enabled=true）。

    生效配置回写旧版单值 settings（rerank_api_url/rerank_api_key/rerank_model），
    kb_common.rag.reranker 运行链路保持不变。
    """
    __tablename__ = "rerank_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    api_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RagflowProfile(Base):
    """多条 RAGFlow 知识库连接配置（多环境切换），只能生效一条（enabled=true）。

    生效配置回写旧版单值 settings（ragflow_base_url/ragflow_api_key），
    ragflow_route / ragflow_client / 知识源同步链路保持不变。
    """
    __tablename__ = "ragflow_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EmbeddingProfile(Base):
    """多条 Embedding 向量模型配置（多环境/多模型切换），只能生效一条（enabled=true）。

    生效配置回写旧版单值 settings（embedding_base_url/embedding_api_key/embedding_model），
    kb_common.rag.embedder（本地 RAG 入库/检索）链路保持不变。
    """
    __tablename__ = "embedding_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    api_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatSession(Base):
    """智能问答会话：每个会话独立隔离，拥有各自的记忆文档。"""
    __tablename__ = "chat_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    # 会话记忆文档：持续浓缩的对话摘要，用于多轮改写与长期记忆
    memory: Mapped[str] = mapped_column(Text, default="")
    last_query: Mapped[str] = mapped_column(Text, default="")
    last_answer: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatMessage(Base):
    """会话内的单条消息（user / assistant）。"""
    __tablename__ = "chat_messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))           # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(ARRAY(String), default=list)  # 引用文档标题
    meta: Mapped[str | None] = mapped_column(Text)          # 元信息（模型/召回数等）
    detail: Mapped[dict | None] = mapped_column(JSON)       # 回答链路/召回片段 {steps, retrieval, model, duration_ms}
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UsageLog(Base):
    """知识调用日志：记录每次检索/问答，用于运营看板统计召回数、调用量、热门知识。"""
    __tablename__ = "usage_logs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    scene: Mapped[str] = mapped_column(String(20))   # search | chat
    query: Mapped[str | None] = mapped_column(Text)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)           # 召回条数
    hit_document_ids: Mapped[list] = mapped_column(ARRAY(String), default=list)  # 命中的文档 ID
    hit_document_titles: Mapped[list] = mapped_column(ARRAY(String), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class OperateMetricRecord(Base):
    """运营看板指标流水：每日 0 点定时（或手动刷新）写入，展示取各指标最新一条。

    metric_key: storage（知识存储总量）| knowledge_count（知识数量+分布）| hot_docs（热门知识Top20）
    value: 指标内容 JSON（含 metric_date/created_at 口径信息）
    """
    __tablename__ = "operate_metric_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_key: Mapped[str] = mapped_column(String(32), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[dict] = mapped_column(JSON)
    trigger: Mapped[str] = mapped_column(String(16), default="daily")  # daily | manual
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class QaFeedback(Base):
    """问答反馈：点赞(helpful)/纠错(correct)/没找到(notfound)。纠错类进入「知识纠错」工单流。"""
    __tablename__ = "qa_feedbacks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)  # 助手消息 ID
    feedback_type: Mapped[str] = mapped_column(String(20), index=True)  # helpful | correct | notfound
    knowledge_title: Mapped[str | None] = mapped_column(String(500))    # 知识标题（纠错时可指定）
    knowledge_url: Mapped[str | None] = mapped_column(String(1000))     # 知识链接（钉钉文档/Dify/预览URL）
    error_type: Mapped[str | None] = mapped_column(String(32))          # 内容错误/知识重复/知识过期/知识难理解/知识不完整/知识模板错误/其它
    content: Mapped[str | None] = mapped_column(Text)                   # 纠错/反馈说明
    question: Mapped[str | None] = mapped_column(Text)                  # 当时的问题（冗余，便于工单展示）
    # 工单处理
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | processing | resolved | closed
    handler_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    handler_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# ===== 知识源登记（企业知识库注册表）=====
# 所有知识库（钉钉知识库 / Dify 知识库 / 业务系统等）统一在此登记。
# 系统内所有「选择知识库」的地方均从此表取数，同步源等只能引用已登记的知识库。
# 通过知识库 ID 可进一步获取其下的目录与文档。

class KnowledgeSource(Base):
    """知识源登记：企业知识库注册表。

    source_type: dingtalk_workspace（钉钉知识库）| dify_dataset（Dify 知识库）|
                 business_system（业务系统）
    external_id: 外部系统中的知识库标识（钉钉 workspace_id / Dify dataset_id / 业务系统标识）
    config: 类型特定配置（JSON），如钉钉 root_node_id、业务系统 base_url 等
    """
    __tablename__ = "knowledge_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ===== 知识库（检索抽象层）=====
# 智能体检索用的统一「知识库」：只做外部检索引擎库的镜像登记（platform + dataset_id），
# 不支持导入/解析新文档；检索时按 platform 由抽象层决定各自检索策略。
# 与 knowledge_sources（推送路径定义）相互独立。

class KnowledgeLibrary(Base):
    """知识库镜像：检索抽象层注册表（仅 RAGFlow / Dify）。

    platform: dify | ragflow
    dataset_id: 对应平台的数据集 ID（同平台内唯一）
    """
    __tablename__ = "knowledge_libraries"
    __table_args__ = (UniqueConstraint("platform", "dataset_id", name="uq_knowledge_library_platform_dataset"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    library_type: Mapped[str] = mapped_column(String(20), server_default="external", nullable=False)
    creator: Mapped[str | None] = mapped_column(String(100))
    engine_config: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class SensitiveItem(Base):
    """敏感信息：知识应用侧的敏感内容登记表（关键词/号码等），供问答与检索链路脱敏管控。

    content: 敏感信息内容（全文唯一，避免重复登记）
    type:    类型（phone|id_card|bank_card|email|custom）
    status:  enabled=True 启用管控 / False 停用（保留条目不参与管控）
    """
    __tablename__ = "sensitive_items"
    __table_args__ = (UniqueConstraint("content", name="uq_sensitive_items_content"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class MaskingPolicy(Base):
    """检索返回脱敏策略：条件→识别→动作→执行节点→兜底。

    - scope_type/scope_id：作用域（global 全局 / library 知识库镜像 / kb 本地知识库），继承叠加
    - user_roles/scenes：触发条件（空 = 全部角色 / 全部场景；场景 search|chat）
    - regex_rules：自定义正则 [{"pattern","label","entity_type"}]
    - dict_types：敏感词典来源（sensitive_items.type 列表，空 = 不启用词典）
    - context_rules：上下文规则 [{"keyword","entity_type"}]（关键词邻近金额）
    - actions：entity_type -> partial|generalize|replace|hash|truncate|reject
    - pre_llm_enabled/post_output_enabled：执行节点（送LLM前脱敏 / 输出后二次过滤）
    - failure_strategy：引擎失败兜底 block|non_sensitive|deny
    多策略命中同一实体类型时取最严格动作（reject>hash>replace>truncate>generalize>partial）。
    """
    __tablename__ = "masking_policies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), default="global", nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), server_default="", nullable=False)  # global 为空
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)  # 小者优先
    user_roles: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    scenes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    regex_rules: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    dict_types: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    context_rules: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    actions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    pre_llm_enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    post_output_enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    failure_strategy: Mapped[str] = mapped_column(String(20), default="non_sensitive", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class MaskingExemption(Base):
    """脱敏豁免：绑定人+范围+实体类型+有效期，到期自动失效；明文访问走审计。"""
    __tablename__ = "masking_exemptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope_type: Mapped[str] = mapped_column(String(16), default="global", nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), server_default="", nullable=False)
    entity_types: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # 空 = 全部
    reason: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    granted_by: Mapped[str] = mapped_column(String(100), server_default="", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 空 = 永久
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class MaskingLog(Base):
    """脱敏审计日志：记录策略命中/豁免/阻断（日志自身脱敏——只记规则标签与数量，不存原文实体）。"""
    __tablename__ = "masking_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    username: Mapped[str] = mapped_column(String(100), server_default="", nullable=False)
    scene: Mapped[str] = mapped_column(String(20), nullable=False)      # search|chat|sandbox
    node: Mapped[str] = mapped_column(String(20), nullable=False)       # pre_llm|post_output
    query: Mapped[str] = mapped_column(String(500), server_default="", nullable=False)  # 已脱敏后的查询
    policy_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    rule_hits: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # [{"rule","entity_type","count"}]
    masked_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    exempted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False, index=True)


class DingtalkFolderStat(Base):
    """钉钉知识库文件夹统计快照（知识缺口页数据源）。

    由「刷新」按钮触发后台全量遍历写入；查询接口只读快照，不实时调用钉钉。
    一个文件夹一行，document_count 为该文件夹直属文档数（在线文档+本地上传文件）。
    """
    __tablename__ = "dingtalk_folder_stats"
    __table_args__ = (UniqueConstraint("external_id", "node_id", name="uq_dt_folder_node"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)  # 知识源 external_id
    node_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 钉钉文件夹节点 ID
    path: Mapped[str] = mapped_column(String(1000), server_default="", nullable=False)  # 不带前导斜杠；根为空串
    document_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    owner: Mapped[str] = mapped_column(String(200), server_default="", nullable=False)  # 手动维护/批量导入，刷新快照时保留
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class DingtalkFileSnapshot(Base):
    """钉钉知识库文件列表快照（知识加工 → 钉钉知识页数据源）。

    全量遍历操作人可见的全部团队知识库需 25–30 分钟、约 4,500 次节点请求，
    因此**只在用户手动点「刷新」时**执行；列表接口只读本快照，
    遍历结果持久化在此表（进程重启 / 容器重建后仍可用），只保留最新一份：
    payload 为文件字典列表的 JSON（含创建人姓名，前端展示无需再调用钉钉）。
    """
    __tablename__ = "dingtalk_file_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class DingtalkFileReview(Base):
    """钉钉文件入库审核记录（入库审核页审核状态数据源）。

    按钉钉节点 ID 独立记录审核状态与审核人，不与 dingtalk_file_snapshots 耦合：
    快照刷新/覆盖写入不影响已审核结果。review_status 取值：通过/待确认/待更正，
    空串视为未审核。
    """
    __tablename__ = "dingtalk_file_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)  # 钉钉节点 ID
    workspace_id: Mapped[str] = mapped_column(String(128), server_default="", nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), server_default="", nullable=False)
    reviewer: Mapped[str] = mapped_column(String(100), server_default="", nullable=False)  # 执行审核动作的用户名
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


# ===== 钉钉知识库 → Dify 定时增量同步（合并自 DingDingKonwledgePipeline）=====
# 每个同步源自带 cron 字段，不保留独立 jobs 表。表结构由 alembic 0011 创建。

class SyncSource(Base):
    """同步源：一个钉钉知识库目录 → 一个外部知识库数据集，含 cron 定时表达式。

    backend_type 决定目标引擎：dify | ragflow。为最小化改动，目标库标识复用
    dify_dataset_id / dify_dataset_name 两列（对 RAGFlow 存的是其 dataset_id/name），
    远端文档 ID 复用 SyncDocumentMapping.dify_document_id（对 RAGFlow 存其 document_id）；
    这样检索召回时的钉钉链接回填对两种引擎自动生效。
    """
    __tablename__ = "sync_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    root_node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    start_dir: Mapped[str] = mapped_column(String(500), server_default="", nullable=False)
    # 目标引擎：dify（默认，历史行为不变）| ragflow
    backend_type: Mapped[str] = mapped_column(String(16), server_default="dify", nullable=False)
    dify_dataset_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dify_dataset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delete_policy: Mapped[str] = mapped_column(String(10), server_default="keep", nullable=False)  # keep | sync
    cron: Mapped[str] = mapped_column(String(120), server_default="0 2 * * *", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    # 流水线数据集（runtime_mode=rag_pipeline）的 input form 变量值，JSON 对象字符串，
    # 如 {"max_chunk_length": 1024, "parent_mode": "full_doc"}。
    # Dify 的 pipeline/run 接口要求 inputs 携带流水线定义的必填变量，缺失会报 500
    # "xxx is required in input form"。普通数据集忽略此字段。
    pipeline_inputs: Mapped[str] = mapped_column(Text, server_default="{}", nullable=False)
    # 节点白名单：JSON 数组字符串，限定只同步这些钉钉文档节点（「添加知识」选择
    # 指定文档 + 自动同步时写入）；为空数组表示同步整棵目录树（历史行为）。
    node_whitelist: Mapped[str] = mapped_column(Text, server_default="[]", nullable=False)
    # XXL-Job 调度中心作业 ID（迁移 0029）：同步源为作业配置唯一事实源，
    # 后端经 admin API 幂等增删改作业，作业 ID 回存本列。
    xxl_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 同步身份归属：定时/后台同步用该用户的钉钉 unionId 调钉钉 API（迁移 0043）；
    # 为空或该用户未绑定钉钉时回退全局 dingtalk_operator_union_id 服务账号。
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class SyncRun(Base):
    """单次同步运行记录。"""
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), server_default="manual", nullable=False)  # manual | schedule
    # 触发人：定时为「系统」，手动为登录用户名；同步队列的逐文档任务行继承该值。
    operator: Mapped[str] = mapped_column(String(100), server_default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="running", nullable=False)  # running | success | partial | failed
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    deleted_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    message: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    # 本次运行调钉钉 API 用的身份来源：owner_binding | global_fallback（迁移 0043）
    operator_source: Mapped[str] = mapped_column(String(20), server_default="", nullable=False)


class SyncDocumentMapping(Base):
    """钉钉节点 ↔ Dify 文档映射，用于增量同步。"""
    __tablename__ = "sync_document_mappings"
    __table_args__ = (UniqueConstraint("source_id", "node_id", name="uq_sync_source_node"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_node_id: Mapped[str] = mapped_column(String(128), server_default="", nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1000), server_default="", nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)  # DOCUMENT | ALIDOC
    meta_hash: Mapped[str] = mapped_column(String(128), server_default="", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), server_default="", nullable=False)
    local_path: Mapped[str] = mapped_column(String(1000), server_default="", nullable=False)
    dify_document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dify_batch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 预演页可单独停用某个文档；该状态必须跨后续手动/定时同步保留。
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text('true'), nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="pending", nullable=False)  # pending | synced | error
    error: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class SyncFailure(Base):
    """同步失败项（待重试清单）。"""
    __tablename__ = "sync_failures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 可空：任务级重试（同步队列）产生的失败记录不归属任何同步运行。
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(500), server_default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class SyncTask(Base):
    """同步队列：逐文档任务记录（知识同步过程中的任务列表）。

    状态流转：pending（本轮已排队）→ running（下载/上传中）→ success | failed；
    删除类动作（钉钉侧已删除）同样记一行，action=delete。
    失败任务支持任务级重试：重试复用同一行（retry_count 累加），不整源重跑。
    """
    __tablename__ = "sync_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), server_default="sync", nullable=False)  # sync
    action: Mapped[str] = mapped_column(String(20), server_default="create", nullable=False)  # create | update | delete
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(32), server_default="", nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dataset_id: Mapped[str] = mapped_column(String(128), server_default="", nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(300), server_default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="pending", nullable=False, index=True)  # pending | running | success | failed
    error: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), server_default="manual", nullable=False)  # manual | schedule
    operator: Mapped[str] = mapped_column(String(100), server_default="", nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class CollectionTransfer(Base):
    """Durable per-document results for manual uploads and selected DingTalk files."""
    __tablename__ = "collection_transfers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    document_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SyncLog(Base):
    """同步运行日志。"""
    __tablename__ = "sync_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=True)
    level: Mapped[str] = mapped_column(String(10), server_default="INFO", nullable=False)  # INFO | ERROR | WARNING
    message: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


# ===== 知识加工：打标/摘要/知识关系 =====

class ProcessedDocument(Base):
    """文档加工记录：对已进入 Dify 的文档进行 AI 打标与摘要生成。"""
    __tablename__ = "processed_documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    tags: Mapped[list] = mapped_column(ARRAY(String(64)), default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list] = mapped_column(ARRAY(String(64)), default=list)
    doc_type: Mapped[str] = mapped_column(String(32), default="")
    process_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|processing|completed|failed
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class KnowledgeRelation(Base):
    """知识关系：文档间的语义关联（引用/相似/因果/包含/并列/前置知识等）。"""
    __tablename__ = "knowledge_relations"
    __table_args__ = (UniqueConstraint("source_doc_id", "target_doc_id", "relation_type",
                                       name="uq_knowledge_relation_triple"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_doc_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_doc_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KnowledgeTag(Base):
    """标签库：统一维护系统中使用的标签。"""
    __tablename__ = "knowledge_tags"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    color: Mapped[str] = mapped_column(String(16), default="#409EFF")
    count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DifyDingtalkDocMapping(Base):
    """Dify 文档 ↔ 钉钉知识库文档映射。

    钉钉文档同步到 Dify 后记录此映射，检索召回 Dify 分段时据此回填
    钉钉原始文档链接（alidocs），点击引用来源可跳转钉钉知识库预览。
    """
    __tablename__ = "dify_dingtalk_doc_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dify_dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dify_document_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    dingtalk_node_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dingtalk_url: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    name: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    # file=直接上传原文件；text=大文件经 MinerU 解析后 create-by-text
    method: Mapped[str] = mapped_column(String(10), nullable=False, server_default="file")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class DingtalkBinding(Base):
    """钉钉身份 ↔ 本地用户绑定（H5 免登 / 机器人消息归因）。

    首次免登自动建档：users.username=dd_{dt_userid}、role=viewer（仅问答可见）；
    绑定表记录 corp/userid/unionid 与钉钉真实姓名，供展示与运营归因。
    """
    __tablename__ = "dingtalk_bindings"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               unique=True, index=True)
    corp_id: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    dt_userid: Mapped[str] = mapped_column(String(128), nullable=False)
    dt_unionid: Mapped[str | None] = mapped_column(String(128))
    dt_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (UniqueConstraint("corp_id", "dt_userid", name="uq_dingtalk_binding_corp_user"),)


class LibraryDocument(Base):
    """Project-owned originals and metadata; engine identifiers stay behind the adapter."""
    __tablename__ = "library_documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[int] = mapped_column(ForeignKey("knowledge_libraries.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    storage_path: Mapped[str] = mapped_column(String(1000))
    size: Mapped[int] = mapped_column(BigInteger)
    engine_document_id: Mapped[str | None] = mapped_column(String(128))
    # MinerU 解析任务 ID（engine_document_id 存三步上传得到的 file_id）
    engine_job_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default="UPLOADED")
    progress: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # 文档级检索开关：禁用时分段从检索通道摘除（数据保留，启用即恢复）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 文档级索引设置覆盖：{processing, strategy, enhancements, type_rules}；缺省回退库级 engine_config
    engine_config: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}', nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="local", server_default="local")
    # 训练/解析完成时间（COMPLETED 时写入）
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime)
    tags: Mapped[list] = mapped_column(ARRAY(String(64)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class LibraryChunk(Base):
    """文档库分段：MinerU 解析产物按库分段规则本地切块的结果。

    父子分段（Dify parent-child 移植）：parent_id 为空 = 父分段（列表/编辑入口，
    作为召回上下文）；指向父分段 = 子分段（由分段器生成，是检索单元——向量与
    词项打分都作用于子块，命中后返回父块）。child_index 为父块内子块序号
    （父分段为 NULL），供前端稳定展示。
    """
    __tablename__ = "library_chunks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("library_documents.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("library_chunks.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    important_keywords: Mapped[list] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)
    child_index: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ===== 知识治理标准（本地可管理 + 版本 + 审核流转）=====
# 治理标准从「钉钉多维表只读代理」升级为本地可管理：
# - 主表存当前对外展示版本的字段快照（列表查询直接读，无需 join 版本表）
# - 版本表存所有历史版本（草稿/审核中/已发布/已驳回），支持回滚
# 状态流转：
#   新建 → draft → 提交审核 → reviewing → 通过 → published（主表快照更新）
#                              → 驳回 → draft（主表仍显示旧已发布版本）
#   已发布 → 编辑 → 新建 draft 版本（主表不变，列表仍显示已发布版本）

class GovernanceStandard(Base):
    """治理标准主表：当前对外展示版本的字段快照。"""
    __tablename__ = "governance_standards"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_type: Mapped[str] = mapped_column(String(100), default="")
    code: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(32), default="")
    effective_date: Mapped[str] = mapped_column(String(20), default="")
    link: Mapped[str] = mapped_column(String(1000), default="")
    maintainer: Mapped[str] = mapped_column(String(200), default="")
    # 当前已发布版本 ID（空=尚无已发布版本，列表展示最新草稿）
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # 最新版本 ID（可能是草稿/审核中）
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # 最新版本状态：draft | reviewing | published
    latest_status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class GovernanceStandardVersion(Base):
    """治理标准版本历史：每个版本一条完整字段快照。"""
    __tablename__ = "governance_standard_versions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    standard_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("governance_standards.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    doc_type: Mapped[str] = mapped_column(String(100), default="")
    code: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(32), default="")
    effective_date: Mapped[str] = mapped_column(String(20), default="")
    link: Mapped[str] = mapped_column(String(1000), default="")
    maintainer: Mapped[str] = mapped_column(String(200), default="")
    # draft | reviewing | published | rejected
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    review_comment: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(100), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
