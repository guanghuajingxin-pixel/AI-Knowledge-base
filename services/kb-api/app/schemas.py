from pydantic import BaseModel, ConfigDict, field_validator
import uuid
from datetime import datetime
from typing import Any


class KbIn(BaseModel):
    name: str
    description: str | None = None
    kb_type: str = "DOCUMENT"
    chunk_strategy: str = "FIXED_SIZE"
    chunk_size: int = 512
    chunk_overlap: int = 150
    delimiter: str | None = None


class KbOut(KbIn):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime
    es_index_name: str
    document_count: int = 0
    owner_name: str = ""
    status: str = "正常"
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class DirIn(BaseModel):
    name: str
    parent_id: uuid.UUID | None = None


class DirOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    sort_order: int = 0
    document_count: int = 0
    children: list["DirOut"] = []

    class Config:
        from_attributes = True


class KcDocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    kb_name: str = ""
    kb_type: str = ""
    directory_id: uuid.UUID | None = None
    directory_name: str | None = None
    original_filename: str
    file_type: str
    file_size: int = 0
    status: str
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class TrashItemOut(BaseModel):
    id: uuid.UUID
    original_filename: str
    directory_name: str | None = None
    kb_name: str = ""
    operator_name: str = ""
    deleted_at: datetime | None = None
    remaining_days: int = 0


class TaskStatsOut(BaseModel):
    total: int = 0
    executing: int = 0
    completed: int = 0
    failed: int = 0


class SegmentOut(BaseModel):
    id: uuid.UUID
    chunk_index: int
    content: str
    content_hash: str | None
    token_count: int
    page_number: int | None


# ===== 知识源登记（企业知识库注册表）=====
# 可检索/可同步的外部知识库引擎类型 + 钉钉数据源 + 业务系统占位。
VALID_SOURCE_TYPES = {
    "dingtalk_workspace",  # 钉钉知识库（数据源）
    "dify_dataset",        # Dify 知识库（检索/同步目标）
    "ragflow_dataset",     # RAGFlow 知识库（检索/同步目标，与 Dify 并列）
    "business_system",     # 业务系统（占位）
}


def _validate_source_type(v: str | None) -> str | None:
    if v is not None and v not in VALID_SOURCE_TYPES:
        raise ValueError(f"未知知识库类型: {v}（可选：{', '.join(sorted(VALID_SOURCE_TYPES))}）")
    return v


class KnowledgeSourceBase(BaseModel):
    name: str
    source_type: str  # dingtalk_workspace | dify_dataset | ragflow_dataset | business_system
    external_id: str
    description: str = ""
    config: dict | None = None
    enabled: bool = True


class KnowledgeSourceCreate(KnowledgeSourceBase):
    @field_validator("source_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return _validate_source_type(v) or v


class KnowledgeSourceUpdate(BaseModel):
    name: str | None = None
    source_type: str | None = None
    external_id: str | None = None
    description: str | None = None
    config: dict | None = None
    enabled: bool | None = None

    @field_validator("source_type")
    @classmethod
    def _check_type(cls, v: str | None) -> str | None:
        return _validate_source_type(v)


class KnowledgeSourceOut(KnowledgeSourceBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


# ===== 钉钉知识库 → Dify 定时增量同步 =====
from pydantic import Field


class SyncSourceBase(BaseModel):
    name: str
    workspace_id: str
    root_node_id: str
    start_dir: str = ""
    # 目标引擎：dify | ragflow | library（默认 dify，历史行为不变）
    backend_type: str = Field(default="dify", pattern="^(dify|ragflow|library)$")
    # 目标知识库标识（对 dify 存 dataset_id/name，对 ragflow 存其 dataset_id/name）
    dify_dataset_name: str
    dify_dataset_id: str | None = None
    delete_policy: str = Field(default="keep", pattern="^(keep|sync)$")
    cron: str = "0 2 * * *"
    enabled: bool = True
    # 流水线数据集的 input form 变量值（分段参数），如 {"max_chunk_length": 1024}。
    # 普通数据集忽略；流水线数据集缺失必填变量时 Dify 会报 500。RAGFlow 忽略此字段。
    pipeline_inputs: dict[str, Any] = Field(default_factory=dict)
    # 节点白名单：限定只同步这些钉钉文档节点（「添加知识」选择指定文档 + 自动同步）；
    # 空数组表示同步整棵目录树（历史行为）。
    node_whitelist: list[str] = Field(default_factory=list)
    # 同步身份归属用户：定时/后台同步用该用户的钉钉 unionId 调钉钉 API；
    # 为空回退全局服务账号。创建时不传默认取当前登录用户。
    owner_user_id: str | None = None


class SyncSourceCreate(SyncSourceBase):
    pass


class SyncSourceTest(SyncSourceBase):
    """保存前连接测试使用的同步源草稿。"""
    pass


class SyncSourceUpdate(BaseModel):
    name: str | None = None
    workspace_id: str | None = None
    root_node_id: str | None = None
    start_dir: str | None = None
    backend_type: str | None = Field(default=None, pattern="^(dify|ragflow|library)$")
    dify_dataset_name: str | None = None
    dify_dataset_id: str | None = None
    delete_policy: str | None = Field(default=None, pattern="^(keep|sync)$")
    cron: str | None = None
    enabled: bool | None = None
    pipeline_inputs: dict[str, Any] | None = None
    node_whitelist: list[str] | None = None
    owner_user_id: str | None = None


class SyncPreviewItemSetting(BaseModel):
    node_id: str
    name: str = ""
    category: str = "DOCUMENT"
    enabled: bool


class SyncPreviewSettingsUpdate(BaseModel):
    items: list[SyncPreviewItemSetting]


class SyncSourceOut(SyncSourceBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    dify_dataset_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("pipeline_inputs", mode="before")
    @classmethod
    def _parse_pipeline_inputs(cls, value: Any) -> dict[str, Any]:
        """ORM 里 pipeline_inputs 是 Text 存的 JSON 字符串，序列化时转回 dict。"""
        import json as _json
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return value
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}

    @field_validator("node_whitelist", mode="before")
    @classmethod
    def _parse_node_whitelist(cls, value: Any) -> list[str]:
        """ORM 里 node_whitelist 是 Text 存的 JSON 字符串，序列化时转回 list。"""
        import json as _json
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []

    @field_validator("owner_user_id", mode="before")
    @classmethod
    def _coerce_owner_user_id(cls, value: Any) -> str | None:
        """ORM 里 owner_user_id 是 UUID，序列化时转为字符串。"""
        if value is None:
            return None
        return str(value)


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    total: int
    created_count: int
    updated_count: int
    deleted_count: int
    failed_count: int
    message: str
    # 本次运行调钉钉 API 的身份来源：owner_binding | global_fallback | ""（历史数据）
    operator_source: str = ""


class SyncFailureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int | None
    source_id: int
    node_id: str | None
    name: str
    error: str
    created_at: datetime


class SyncTaskOut(BaseModel):
    """同步队列：逐文档任务记录输出。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int | None
    source_id: int | None
    kind: str
    action: str
    node_id: str | None
    name: str
    file_ext: str
    file_size: int | None
    dataset_id: str
    dataset_name: str
    status: str
    error: str
    trigger: str
    operator: str
    retry_count: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class SyncTaskBatchIn(BaseModel):
    ids: list[int]


class SyncLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int | None
    level: str
    message: str
    created_at: datetime
