"""同步模块凭据解析：DB 系统配置（Setting 表）优先，回退 .env Settings。

平台钉钉/Dify 凭据由 系统配置页（settings_route）维护在 DB 中，
.env 里通常为空。同步路由与引擎必须统一走此解析，否则拿到空凭据，
钉钉会报 invalidClientIdOrSecret。
"""
from __future__ import annotations

from sqlalchemy import select

from kb_common.config import get_settings
from kb_common.models import Setting

# 同步模块关心的可配置项（与 settings_route.KEYS 保持一致）
SYNC_SETTING_KEYS = (
    "dingtalk_app_key",
    "dingtalk_app_secret",
    "dingtalk_operator_union_id",
    "dify_base_url",
    "dify_api_key",
    "dify_upload_max_mb",
    "ragflow_base_url",
    "ragflow_api_key",
)


def load_sync_settings(db) -> dict[str, str]:
    """返回 key→value；db 为同步 Session（SyncSessionLocal）。"""
    s = get_settings()
    vals: dict[str, str] = {k: (getattr(s, k, "") or "") for k in SYNC_SETTING_KEYS}
    try:
        rows = db.execute(
            select(Setting).where(Setting.key.in_(SYNC_SETTING_KEYS))
        ).scalars().all()
        for row in rows:
            if row.value and row.value.strip():
                vals[row.key] = row.value.strip()
    except Exception:  # noqa: BLE001 — DB 查询失败时退回 .env
        pass
    return vals


def make_dingtalk_client(db, operator_union_id: str | None = None):
    """用解析后的凭据构建同步 DingTalkClient。

    operator_union_id：按同步源 owner 维度传入（权限与 owner 一致）；
    不传回退全局 dingtalk_operator_union_id 服务账号（历史行为）。
    """
    from app.services.sync.dingtalk_sync_client import DingTalkClient

    vals = load_sync_settings(db)
    return DingTalkClient(
        vals["dingtalk_app_key"],
        vals["dingtalk_app_secret"],
        operator_union_id or vals["dingtalk_operator_union_id"],
    )


def make_dify_client(db):
    """用解析后的凭据构建同步 DifyClient。"""
    from app.services.sync.dify_sync_client import DifyClient

    vals = load_sync_settings(db)
    get_settings().dify_upload_max_mb = int(vals["dify_upload_max_mb"])
    return DifyClient(vals["dify_base_url"], vals["dify_api_key"])


def make_ragflow_client(db):
    """用解析后的凭据构建同步 RagflowSyncClient。"""
    from app.services.sync.ragflow_sync_client import RagflowSyncClient

    vals = load_sync_settings(db)
    return RagflowSyncClient(vals["ragflow_base_url"], vals["ragflow_api_key"])


def make_library_backend(source):
    """构建文档库（document-libraries）同步后端。

    目标库 ID 复用 dify_dataset_id 列（backend_type=library 时存本地文档库 ID）。
    source 可为 SyncSource 对象或 dict（路由层创建/更新时）。
    """
    from app.services.sync.library_backend import LibraryBackend
    if isinstance(source, dict):
        dataset_id = source.get("dify_dataset_id") or ""
        src_id = source.get("id", "?")
    else:
        dataset_id = getattr(source, "dify_dataset_id", "") or ""
        src_id = getattr(source, "id", "?")
    try:
        library_id = int(dataset_id)
    except (ValueError, TypeError):
        raise ValueError(f"同步源 {src_id} 的文档库 ID 无效：{dataset_id!r}")
    return LibraryBackend(library_id)


def make_backend(source, db):
    """按同步源的 backend_type 返回目标引擎客户端（dify | ragflow | library）。

    source 可传 SyncSource 对象、dict（路由层创建/更新时），
    或直接传 backend_type 字符串（路由层校验时用，仅 dify/ragflow 支持）。
    各客户端暴露同一套方法面（resolve_dataset/upload_file/update_file/
    delete_document/wait_indexing/close），SyncEngine 无需区分具体引擎；
    Dify 专属的知识流水线逻辑由 engine 用 backend_type=='dify' 守卫。
    library 后端写入本地文档库（MinIO + LibraryDocument + MinerU 解析）。
    """
    if isinstance(source, str):
        backend_type = source
        target = source
    else:
        backend_type = (getattr(source, "backend_type", None)
                        if not isinstance(source, dict)
                        else source.get("backend_type")) or "dify"
        target = source
    backend_type = backend_type.strip().lower()
    if backend_type == "ragflow":
        return make_ragflow_client(db)
    if backend_type == "library":
        return make_library_backend(target)
    return make_dify_client(db)
