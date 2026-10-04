"""知识中心跨知识库聚合路由"""
import asyncio
import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, case, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Literal
from kb_common.database import get_session
from kb_common.models import (
    KnowledgeBase, Directory, Document, User, KnowledgeSource, DingtalkFileSnapshot,
    DingtalkFolderStat, DingtalkFileReview, KnowledgeLibrary, LibraryDocument,
    SyncDocumentMapping, SyncSource,
)
from kb_common.clients import es_client
from app.schemas import (
    DirOut, KcDocumentOut, TrashItemOut, TaskStatsOut,
    KnowledgeSourceCreate, KnowledgeSourceOut, KnowledgeSourceUpdate,
)
from app.deps import get_current_user, require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge-center", tags=["knowledge-center"])

# 回收站自动清理天数
TRASH_RETENTION_DAYS = 20

# ===== 钉钉知识库文件列表快照 =====
# 全量遍历操作人可见的全部团队知识库约需 25–30 分钟、约 4,500 次节点请求，
# 因此**只在用户手动点「刷新」时**执行（_trigger_dingtalk_refresh 仅由
# refresh=true 触发）。列表接口只读快照，不调用钉钉：
#   - 持久层：dingtalk_file_snapshots 表（进程重启 / 容器重建后列表仍在，只留最新一份）
#   - 内存层：首次请求从库载入，避免每次请求都解析数 MB JSON；刷新成功后同步更新
_dingtalk_cache: dict = {"files": None, "loading": False, "error": None, "cached_at": None}
_snapshot_loaded = False  # 进程内是否已从数据库载入快照

# 历史遗留的磁盘快照（0023 之前落盘于 /tmp）：仅用于一次性导入数据库，避免升级后列表为空
_LEGACY_SNAPSHOT_PATH = Path(os.getenv("KGE_DINGTALK_SNAPSHOT", "/tmp/kge_dingtalk_files.json"))


def _to_utc_iso(dt: datetime | None) -> str | None:
    """本地时间 → UTC ISO（带时区），供前端按浏览器时区展示。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 库内为服务器本地时间
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


async def _import_legacy_snapshot(s: AsyncSession) -> bool:
    """一次性把历史 /tmp JSON 快照导入数据库（导入成功后不再使用该文件）。"""
    try:
        if not _LEGACY_SNAPSHOT_PATH.exists():
            return False
        data = json.loads(_LEGACY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        files = data.get("files") or []
        if not files:
            return False
        fetched_at = datetime.fromtimestamp(float(data.get("fetched_at") or 0))
        s.add(DingtalkFileSnapshot(fetched_at=fetched_at, file_count=len(files),
                                   payload=json.dumps(files, ensure_ascii=False)))
        await s.commit()
        logger.info("历史钉钉文件快照已导入数据库：%d 个文件（%s）", len(files), fetched_at)
        return True
    except Exception as e:
        await s.rollback()
        logger.warning("历史钉钉文件快照导入失败：%s", e)
        return False


async def ensure_dingtalk_files_loaded(s: AsyncSession) -> None:
    """确保进程内已载入持久化快照（首次请求时读库，不触发钉钉遍历）。

    尚无快照时保持未加载状态，后续请求继续尝试，使其他进程/刷新任务写入的快照能被发现。
    """
    global _snapshot_loaded
    if _snapshot_loaded:
        return
    row = (await s.execute(
        select(DingtalkFileSnapshot).order_by(DingtalkFileSnapshot.id.desc()).limit(1)
    )).scalar_one_or_none()
    if row is None:
        if await _import_legacy_snapshot(s):
            row = (await s.execute(
                select(DingtalkFileSnapshot).order_by(DingtalkFileSnapshot.id.desc()).limit(1)
            )).scalar_one_or_none()
        if row is None:
            return
    try:
        files = json.loads(row.payload or "[]")
    except Exception as e:
        logger.warning("钉钉文件快照解析失败：%s", e)
        return
    _dingtalk_cache["files"] = files
    _dingtalk_cache["cached_at"] = row.fetched_at
    _snapshot_loaded = True
    logger.info("钉钉文件快照载入完成：%d 个文件（同步于 %s）", len(files), row.fetched_at)


async def _write_dingtalk_snapshot(files: list, fetched_at: datetime) -> None:
    """覆盖写入持久化快照（表内只保留最新一份）。"""
    from kb_common.database import SessionLocal

    payload = json.dumps(files, ensure_ascii=False)
    async with SessionLocal() as s:
        await s.execute(delete(DingtalkFileSnapshot))
        s.add(DingtalkFileSnapshot(fetched_at=fetched_at, file_count=len(files), payload=payload))
        await s.commit()


async def _refresh_dingtalk_files() -> None:
    """手动刷新触发的后台任务：全量遍历钉钉 → 解析创建人姓名 → 持久化快照 + 更新内存缓存。

    创建人姓名在遍历结束时一次性解析并写入快照，列表接口因此无需再调用钉钉通讯录接口
    （钉钉配置异常时也能展示已持久化的列表）。
    """
    from kb_common.clients import dingtalk_client
    global _snapshot_loaded
    try:
        await dingtalk_client.sync_runtime_config()
        files = await dingtalk_client.get_all_knowledge_files()
        creator_ids = list({f.get("creator_id") for f in files if f.get("creator_id")})
        if creator_ids:
            name_map = await dingtalk_client.get_user_name_map(creator_ids)
            for f in files:
                f["creator_name"] = name_map.get(f.get("creator_id") or "", "") or None
        fetched_at = datetime.now()
        await _write_dingtalk_snapshot(files, fetched_at)
        _dingtalk_cache["files"] = files
        _dingtalk_cache["cached_at"] = fetched_at
        _dingtalk_cache["error"] = None
        _snapshot_loaded = True
        logger.info("钉钉知识库文件快照刷新完成：%d 个文件", len(files))
    except Exception as e:
        _dingtalk_cache["error"] = f"钉钉数据拉取失败：{e}"
        logger.warning("钉钉知识库文件快照刷新失败：%s", e)
    finally:
        _dingtalk_cache["loading"] = False


def _trigger_dingtalk_refresh() -> None:
    """启动后台全量遍历（单飞：正在遍历则跳过）。仅由「刷新」按钮调用。"""
    if _dingtalk_cache["loading"]:
        return
    _dingtalk_cache["loading"] = True
    asyncio.create_task(_refresh_dingtalk_files())


def _dingtalk_cache_meta() -> dict:
    """快照元信息：最近一次成功同步时间（UTC ISO），供前端展示数据新鲜度。"""
    return {"cached_at": _to_utc_iso(_dingtalk_cache.get("cached_at"))}


async def _doc_to_kc_dict(r, directory_name: str | None = None) -> dict:
    """将 Document ORM 行转换为知识中心文档字典"""
    return {
        "id": str(r.id) if hasattr(r, 'id') else str(r[0].id) if isinstance(r, tuple) else str(r.id),
    }


@router.get("/directories")
async def get_unified_tree(
    kb_type: str | None = Query(None),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """获取跨知识库的统一目录树"""
    # 查询所有知识库
    kb_q = select(KnowledgeBase).where(KnowledgeBase.kb_type.in_(['DOCUMENT', 'FAQ']))
    if kb_type:
        kb_q = kb_q.where(KnowledgeBase.kb_type == kb_type)
    kbs = (await s.execute(kb_q)).scalars().all()
    if not kbs:
        return []

    # 固定 4 条查询取回全部数据（原来是 1 + 每个知识库 3 条的 N+1 模式：
    # 逐库查文档总数、目录列表、各目录文档数，库一多就把连接占住不放）
    kb_ids = [kb.id for kb in kbs]
    dirs = (await s.execute(
        select(Directory).where(Directory.kb_id.in_(kb_ids)).order_by(Directory.sort_order)
    )).scalars().all()
    kb_doc_counts = dict((await s.execute(
        select(Document.kb_id, func.count(Document.id))
        .where(Document.kb_id.in_(kb_ids), Document.is_deleted == False)  # noqa: E712
        .group_by(Document.kb_id)
    )).all())
    dir_doc_counts: dict[str, int] = {}
    if dirs:
        rows = (await s.execute(
            select(Document.directory_id, func.count(Document.id))
            .where(Document.directory_id.in_([d.id for d in dirs]),
                   Document.is_deleted == False)  # noqa: E712
            .group_by(Document.directory_id)
        )).all()
        dir_doc_counts = {str(dir_id): cnt for dir_id, cnt in rows}

    dirs_by_kb: dict[str, list] = {}
    for d in dirs:
        dirs_by_kb.setdefault(str(d.kb_id), []).append(d)

    result = []
    for kb in kbs:
        kb_dirs = dirs_by_kb.get(str(kb.id), [])

        # 构建目录树
        nodes = {}
        for d in kb_dirs:
            nodes[str(d.id)] = {
                "id": str(d.id),
                "kb_id": str(d.kb_id),
                "parent_id": str(d.parent_id) if d.parent_id else None,
                "name": d.name,
                "sort_order": d.sort_order,
                "document_count": dir_doc_counts.get(str(d.id), 0),
                "children": [],
            }

        roots = []
        for d in kb_dirs:
            node = nodes[str(d.id)]
            if d.parent_id and str(d.parent_id) in nodes:
                nodes[str(d.parent_id)]["children"].append(node)
            else:
                roots.append(node)

        result.append({
            "kb_id": str(kb.id),
            "kb_name": kb.name,
            "kb_type": kb.kb_type,
            "document_count": kb_doc_counts.get(kb.id, 0),
            "children": roots,
        })

    return result


@router.get("/documents")
async def list_documents(
    kb_id: str | None = Query(None),
    directory_id: str | None = Query(None),
    search: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    status: str | None = Query(None),
    kb_type: str | None = Query(None),
    uploader_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """跨知识库文档列表（不含已删除）"""
    # 子查询：目录名
    dir_name_subq = (
        select(Directory.name)
        .where(Directory.id == Document.directory_id)
        .correlate(Document)
        .scalar_subquery()
        .label("directory_name")
    )

    uploader_name_subq = (
        select(User.username)
        .where(User.id == Document.uploader_id)
        .correlate(Document)
        .scalar_subquery()
        .label("uploader_name")
    )

    q = (
        select(
            Document,
            KnowledgeBase.name.label("kb_name"),
            KnowledgeBase.kb_type.label("kb_type"),
            dir_name_subq,
            uploader_name_subq,
        )
        .join(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
        .where(Document.is_deleted == False)
    )

    if kb_id:
        q = q.where(Document.kb_id == kb_id)
    if directory_id:
        q = q.where(Document.directory_id == directory_id)
    if kb_type:
        q = q.where(KnowledgeBase.kb_type == kb_type)
    if search:
        pattern = f"%{search}%"
        q = q.where(Document.original_filename.ilike(pattern))
    if date_from:
        q = q.where(Document.created_at >= date_from)
    if date_to:
        q = q.where(Document.created_at <= date_to)
    if status:
        q = q.where(Document.status == status)
    if uploader_id:
        ids = [uid.strip() for uid in uploader_id.split(",") if uid.strip()]
        if ids:
            q = q.where(Document.uploader_id.in_(ids))

    # 排序
    q = q.order_by(Document.created_at.desc())

    # 计数
    count_q = select(func.count()).select_from(q.subquery())
    total = (await s.execute(count_q)).scalar() or 0

    # 分页
    offset = (page - 1) * size
    q = q.offset(offset).limit(size)
    rows = (await s.execute(q)).all()

    items = []
    for row in rows:
        doc = row[0]
        items.append({
            "id": str(doc.id),
            "kb_id": str(doc.kb_id),
            "kb_name": row.kb_name or "",
            "kb_type": row.kb_type or "",
            "directory_id": str(doc.directory_id) if doc.directory_id else None,
            "directory_name": row.directory_name or None,
            "original_filename": doc.original_filename,
            "file_type": doc.file_type,
            "file_size": doc.file_size,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
            "uploader_id": str(doc.uploader_id) if doc.uploader_id else None,
            "uploader_name": row.uploader_name or None,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        })

    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/uploaders")
async def list_uploaders(
    kb_type: str | None = Query(None),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """获取有文档的创建人列表（用于筛选下拉）"""
    q = (
        select(User.id, User.username)
        .join(Document, Document.uploader_id == User.id)
        .join(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
        .where(Document.is_deleted == False)
        .distinct()
    )
    if kb_type:
        q = q.where(KnowledgeBase.kb_type == kb_type)
    rows = (await s.execute(q)).all()
    return [{"id": str(r.id), "name": r.username} for r in rows]


async def _visible_workspace_ids(user_id) -> tuple[set | None, str | None]:
    """工作区级权限隔离：返回 (可见 workspaceId 集合, 错误文案)。

    (None, None) 表示无需隔离（用户未绑定钉钉，沿用全局快照口径）；
    集合为空且带错误 = 需要隔离但钉钉权限范围拉取失败，调用方按空列表+错误展示
    （权限功能失败时宁可少看，不可越权看全量）。
    """
    from kb_common.clients import dingtalk_client
    from kb_common.database import short_session
    from app.services import dingtalk_operator

    async with short_session() as ss:
        union = await dingtalk_operator.user_union_id(ss, user_id)
    if not union:
        return None, None
    try:
        wss = await dingtalk_client.list_workspaces(operator_union_id=union)
    except Exception as e:  # noqa: BLE001
        logger.warning("拉取登录用户钉钉权限范围失败，本次按空隔离: %s", e)
        return set(), f"拉取钉钉权限范围失败：{e}"
    return {w.get("workspaceId") for w in wss if w.get("workspaceId")}, None


@router.get("/dingtalk/workspaces")
async def list_dingtalk_workspaces(u=Depends(get_current_user)):
    """实时列出操作人可见的钉钉团队知识库（单次 API 调用，含根节点 ID）。

    已绑定钉钉的登录用户用自己的 unionId 取可见范围（钉钉侧权限过滤）；
    未绑定用户回退全局服务账号口径。
    """
    from kb_common.clients import dingtalk_client
    from kb_common.database import short_session
    from app.services import dingtalk_operator

    try:
        await dingtalk_client.sync_runtime_config()
    except Exception as e:
        return {"items": [], "error": f"钉钉配置加载失败：{e}"}
    async with short_session() as ss:
        union = await dingtalk_operator.user_union_id(ss, u.id)
    if not union and not dingtalk_client.is_configured():
        return {"items": [], "error": "钉钉 AppKey/AppSecret/操作人 未配置，请在「系统配置」中填写后重试"}
    try:
        workspaces = await dingtalk_client.list_workspaces(operator_union_id=union)
    except Exception as e:
        return {"items": [], "error": f"获取钉钉知识库列表失败：{e}"}
    items = [
        {"id": w.get("workspaceId"), "name": w.get("name") or "", "root_node_id": w.get("rootNodeId") or ""}
        for w in workspaces
        if w.get("workspaceId") and w.get("type") != "PERSONAL"
    ]
    items.sort(key=lambda x: x["name"])
    return {"items": items, "error": None}


@router.get("/dingtalk/nodes")
async def list_dingtalk_child_nodes(
    parent_node_id: str = Query(..., description="父节点 ID（知识库根节点或目录节点）"),
    u=Depends(get_current_user),
):
    """实时列出某父节点下的直接子节点（单次 API 调用，用于按目录查询文档）。"""
    from kb_common.clients import dingtalk_client
    from kb_common.database import short_session
    from app.services import dingtalk_operator

    if not parent_node_id.strip():
        raise HTTPException(400, "parent_node_id 不能为空")
    try:
        await dingtalk_client.sync_runtime_config()
        async with short_session() as ss:
            union = await dingtalk_operator.user_union_id(ss, u.id)
        if not union and not dingtalk_client.is_configured():
            raise HTTPException(400, "钉钉 AppKey/AppSecret/操作人 未配置，请在「系统配置」中填写后重试")
        nodes = await dingtalk_client.list_nodes(parent_node_id.strip(),
                                                 operator_union_id=union)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"获取钉钉目录内容失败：{e}")
    items = []
    for n in nodes:
        is_folder = n.get("type") == "FOLDER"
        items.append({
            "node_id": n.get("nodeId"),
            "name": n.get("name") or "",
            "is_folder": is_folder,
            "has_children": bool(n.get("hasChildren")),
            "extension": n.get("extension"),
            "size": int(n.get("size") or 0),
            "url": n.get("url"),
            "creator_id": n.get("creatorId") or "",
            "created_at": n.get("createTime"),
            "modified_at": n.get("modifiedTime"),
        })
    return {"items": items}


@router.get("/dingtalk/documents")
async def list_dingtalk_documents(
    workspace_id: str | None = Query(None, description="知识库ID，逗号分隔多选"),
    creator_id: str | None = Query(None, description="创建人userid，逗号分隔多选"),
    search: str | None = Query(None, description="按文件名模糊匹配"),
    directory: str | None = Query(None, description="按目录路径模糊匹配"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    refresh: bool = Query(False, description="为 true（仅「刷新」按钮）时后台重新遍历钉钉并更新快照"),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """钉钉知识库文件列表（读取持久化快照）。

    全量遍历操作人可见的团队知识库约需 25–30 分钟（约 4,500 次节点请求），
    因此**只有手动点「刷新」(refresh=true) 才触发**：接口立即返回并在后台遍历，
    期间前端按 loading=true 轮询，列表继续显示当前快照。
    其余请求（含翻页/筛选/进程重启后首次访问）只读持久化快照（dingtalk_file_snapshots），
    不调用钉钉；创建人姓名在遍历时写入快照，接口不再调用钉钉通讯录。
    """
    from kb_common.clients import dingtalk_client

    config_error = None
    try:
        await dingtalk_client.sync_runtime_config()
        if not dingtalk_client.is_configured():
            config_error = "钉钉 AppKey/AppSecret/操作人 未配置，请在「系统配置」中填写后重试"
    except Exception as e:
        config_error = f"钉钉配置加载失败：{e}"

    if config_error:
        return {
            "items": [], "total": 0, "page": page, "size": size,
            "workspaces": [], "creators": [],
            "loading": False, "error": config_error,
            **_dingtalk_cache_meta(),
        }

    # 仅在手动刷新时启动后台全量遍历（单飞）；其余请求只读快照
    if refresh:
        _trigger_dingtalk_refresh()

    await ensure_dingtalk_files_loaded(s)
    cached_files = _dingtalk_cache["files"]

    # 工作区级权限隔离：已绑定钉钉的用户只看到自己有权限的库（快照为全量口径）
    visible, scope_err = await _visible_workspace_ids(u.id)

    # 尚无快照：首次同步未完成时返回 loading 让前端轮询，否则为空列表（提示点「刷新」）
    if not cached_files:
        return {
            "items": [], "total": 0, "page": page, "size": size,
            "workspaces": [], "creators": [],
            "loading": bool(_dingtalk_cache["loading"]),
            "error": _dingtalk_cache["error"],
            **_dingtalk_cache_meta(),
        }

    files = list(cached_files)
    if visible is not None:
        files = [f for f in files if f.get("workspace_id") in visible]
    # 知识库 / 创建人过滤选项（从快照数据聚合，不额外调用钉钉）
    workspace_map: dict[str, str] = {}
    creator_names: dict[str, str] = {}
    for f in files:
        if f.get("workspace_id") and f.get("workspace_name"):
            workspace_map[f["workspace_id"]] = f["workspace_name"]
        cid = f.get("creator_id")
        if cid and cid not in creator_names:
            creator_names[cid] = f.get("creator_name") or ""

    # 兼容缺少创建人姓名的历史快照：仅在这种情况下才调用钉钉通讯录解析，
    # 解析后回写快照，避免每次服务重启都重新解析（新快照在遍历时已固化姓名）
    missing = [cid for cid, name in creator_names.items() if not name]
    if missing:
        name_map = await dingtalk_client.get_user_name_map(missing)
        creator_names.update({cid: name_map.get(cid) or "" for cid in missing})
        enriched = False
        for f in files:
            cid = f.get("creator_id")
            if cid and not f.get("creator_name") and creator_names.get(cid):
                f["creator_name"] = creator_names[cid]
                enriched = True
        if enriched:
            await _write_dingtalk_snapshot(files, _dingtalk_cache.get("cached_at") or datetime.now())
            _dingtalk_cache["files"] = files
            logger.info("钉钉文件快照已补齐创建人姓名")

    creators = [
        {"id": cid, "name": creator_names.get(cid) or cid}
        for cid in sorted(creator_names, key=lambda x: creator_names.get(x) or x)
    ]
    workspaces = [{"id": wid, "name": name} for wid, name in workspace_map.items()]

    # 过滤
    ws_ids = {v.strip() for v in workspace_id.split(",") if v.strip()} if workspace_id else set()
    cr_ids = {v.strip() for v in creator_id.split(",") if v.strip()} if creator_id else set()
    if ws_ids:
        files = [f for f in files if f.get("workspace_id") in ws_ids]
    if cr_ids:
        files = [f for f in files if f.get("creator_id") in cr_ids]
    if search:
        kw = search.strip().lower()
        files = [f for f in files if kw in (f.get("name") or "").lower()]
    if directory:
        kw = directory.strip()
        files = [f for f in files if kw in (f.get("directory_path") or "")]

    # 按创建时间倒序
    files.sort(key=lambda f: f.get("created_at") or "", reverse=True)

    total = len(files)
    start = (page - 1) * size
    page_items = files[start:start + size]
    # 入库审核状态叠加：仅查当前页文件的审核记录（dingtalk_file_reviews 按 node_id 独立存储）
    review_map: dict[str, DingtalkFileReview] = {}
    node_ids = [f.get("node_id") for f in page_items if f.get("node_id")]
    if node_ids:
        reviews = (await s.execute(
            select(DingtalkFileReview).where(DingtalkFileReview.node_id.in_(node_ids)))).scalars().all()
        review_map = {r.node_id: r for r in reviews}
    items = []
    for f in page_items:
        review = review_map.get(f.get("node_id") or "")
        items.append({
            "node_id": f.get("node_id"),
            "workspace_id": f.get("workspace_id"),
            "workspace_name": f.get("workspace_name") or "",
            "name": f.get("name") or "",
            "directory_path": f.get("directory_path") or "/",
            "category": f.get("category"),
            "extension": f.get("extension"),
            "size": f.get("size") or 0,
            "url": f.get("url"),
            "creator_id": f.get("creator_id") or "",
            "creator_name": f.get("creator_name") or creator_names.get(f.get("creator_id") or "") or None,
            "created_at": f.get("created_at"),
            "modified_at": f.get("modified_at"),
            "review_status": review.review_status if review else "",
            "reviewer": review.reviewer if review else "",
            "reviewed_at": _to_utc_iso(review.reviewed_at) if review else None,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "workspaces": workspaces,
        "creators": creators,
        "loading": _dingtalk_cache["loading"],
        "error": _dingtalk_cache["error"] or scope_err,
        **_dingtalk_cache_meta(),
    }


class DingtalkReviewIn(BaseModel):
    node_id: str = Field(description="钉钉节点 ID")
    workspace_id: str = Field(default="", max_length=128, description="钉钉知识库 ID（留痕）")
    review_status: Literal["通过", "待确认", "待更正"]


@router.put("/dingtalk/documents/review")
async def review_dingtalk_document(
    body: DingtalkReviewIn,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """入库审核：记录/变更钉钉文件的审核状态，审核人=当前登录用户名。

    审核记录按 node_id 独立存储（dingtalk_file_reviews），快照刷新不覆盖；
    重复提交等同变更状态，审核人与时间随之更新。
    """
    node_id = body.node_id.strip()
    if not node_id:
        raise HTTPException(400, "node_id 不能为空")
    row = (await s.execute(
        select(DingtalkFileReview).where(DingtalkFileReview.node_id == node_id))).scalar_one_or_none()
    if row is None:
        row = DingtalkFileReview(node_id=node_id, workspace_id=body.workspace_id or "")
        s.add(row)
    row.workspace_id = body.workspace_id or row.workspace_id
    row.review_status = body.review_status
    row.reviewer = u.username
    row.reviewed_at = datetime.now()
    await s.commit()
    return {"ok": True, "node_id": node_id, "review_status": row.review_status,
            "reviewer": row.reviewer, "reviewed_at": _to_utc_iso(row.reviewed_at)}


@router.get("/documents/{doc_id}")
async def get_document_detail(
    doc_id: _uuid.UUID,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """获取文档详情（含知识库信息）"""
    dir_name_subq = (
        select(Directory.name)
        .where(Directory.id == Document.directory_id)
        .correlate(Document)
        .scalar_subquery()
        .label("directory_name")
    )

    q = (
        select(
            Document,
            KnowledgeBase.name.label("kb_name"),
            KnowledgeBase.kb_type.label("kb_type"),
            dir_name_subq,
        )
        .join(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
        .where(Document.id == doc_id)
    )
    row = (await s.execute(q)).first()
    if not row:
        raise HTTPException(404, "文档不存在")

    doc = row[0]
    return {
        "id": str(doc.id),
        "kb_id": str(doc.kb_id),
        "kb_name": row.kb_name or "",
        "kb_type": row.kb_type or "",
        "directory_id": str(doc.directory_id) if doc.directory_id else None,
        "directory_name": row.directory_name or None,
        "original_filename": doc.original_filename,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
        "status": doc.status,
        "chunk_count": doc.chunk_count,
        "error_message": doc.error_message,
        "storage_path": doc.storage_path,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


@router.get("/recycle-bin")
async def list_recycle_bin(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """回收站列表"""
    dir_name_subq = (
        select(Directory.name)
        .where(Directory.id == Document.directory_id)
        .correlate(Document)
        .scalar_subquery()
        .label("directory_name")
    )

    q = (
        select(
            Document,
            KnowledgeBase.name.label("kb_name"),
            dir_name_subq,
        )
        .join(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
        .where(Document.is_deleted == True)
        .order_by(Document.deleted_at.desc().nulls_last())
    )

    count_q = select(func.count()).select_from(q.subquery())
    total = (await s.execute(count_q)).scalar() or 0

    offset = (page - 1) * size
    q = q.offset(offset).limit(size)
    rows = (await s.execute(q)).all()

    now = datetime.utcnow()
    items = []
    for row in rows:
        doc = row[0]
        deleted_at = doc.deleted_at
        remaining_days = 0
        if deleted_at:
            expiry = deleted_at + timedelta(days=TRASH_RETENTION_DAYS)
            remaining_days = max(0, (expiry - now).days)

        items.append({
            "id": str(doc.id),
            "original_filename": doc.original_filename,
            "directory_name": row.directory_name or None,
            "kb_name": row.kb_name or "",
            "operator_name": "",
            "deleted_at": deleted_at.isoformat() if deleted_at else None,
            "remaining_days": remaining_days,
        })

    return {"items": items, "total": total, "page": page, "size": size}


@router.post("/recycle-bin/{doc_id}/restore")
async def restore_document(
    doc_id: _uuid.UUID,
    u=Depends(require_role("super_admin", "admin")),
    s: AsyncSession = Depends(get_session),
):
    """从回收站恢复文档"""
    doc = await s.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    doc.is_deleted = False
    doc.deleted_at = None
    await s.commit()
    return {"ok": True}


@router.delete("/recycle-bin/{doc_id}")
async def permanent_delete_document(
    doc_id: _uuid.UUID,
    u=Depends(require_role("super_admin", "admin")),
    s: AsyncSession = Depends(get_session),
):
    """永久删除文档（ES + DB）"""
    doc = await s.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")

    # 删除 ES 索引
    kb = await s.get(KnowledgeBase, doc.kb_id)
    if kb:
        try:
            from kb_common.rag.indexer import indexer
            await es_client.es.delete(index=kb.es_index_name, id=str(doc_id), ignore=[404])
        except Exception:
            pass

    await s.delete(doc)
    await s.commit()
    return {"ok": True}


@router.get("/task-stats")
async def get_task_stats(
    kb_type: str | None = Query(None),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """任务队列统计"""
    q = select(
        func.count(Document.id).label("total"),
        func.count(case((Document.status.in_(
            ["PENDING", "PARSING", "CHUNKING", "EMBEDDING", "INDEXING"]
        ), Document.id))).label("executing"),
        func.count(case((Document.status == "COMPLETED", Document.id))).label("completed"),
        func.count(case((Document.status == "FAILED", Document.id))).label("failed"),
    ).where(Document.is_deleted == False)

    if kb_type:
        q = q.join(KnowledgeBase, Document.kb_id == KnowledgeBase.id).where(
            KnowledgeBase.kb_type == kb_type
        )

    row = (await s.execute(q)).first()
    return {
        "total": row.total or 0,
        "executing": row.executing or 0,
        "completed": row.completed or 0,
        "failed": row.failed or 0,
    }


# ========== 知识源登记（企业知识库注册表）==========

@router.get("/knowledge-sources", response_model=list[KnowledgeSourceOut])
async def list_knowledge_sources(
    source_type: str | None = Query(None, description="按类型筛选：dingtalk_workspace | dify_dataset | business_system"),
    enabled_only: bool = Query(False, description="只返回已启用的知识库"),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """知识库列表：系统内所有「选择知识库」的地方均从此接口取数。"""
    q = select(KnowledgeSource)
    if source_type:
        q = q.where(KnowledgeSource.source_type == source_type)
    if enabled_only:
        q = q.where(KnowledgeSource.enabled == True)
    q = q.order_by(KnowledgeSource.source_type, KnowledgeSource.id.desc())
    rows = (await s.execute(q)).scalars().all()
    return rows


@router.post("/knowledge-sources", response_model=KnowledgeSourceOut,
             dependencies=[Depends(require_role("super_admin", "admin"))])
async def create_knowledge_source(
    payload: KnowledgeSourceCreate,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """新增知识库登记。钉钉知识库登记后自动触发知识缺口页的目录快照刷新。"""
    if not payload.name.strip():
        raise HTTPException(422, "知识库名称不能为空")
    if not payload.external_id.strip():
        raise HTTPException(422, "知识库 ID（external_id）不能为空")
    source = KnowledgeSource(**payload.model_dump())
    s.add(source)
    await s.commit()
    await s.refresh(source)
    # 钉钉知识库：登记后立即后台拉取文件夹快照（知识缺口页数据源），
    # 避免用户首次进入该页时还要手动点刷新。
    if source.source_type == "dingtalk_workspace":
        import asyncio
        from app.routes.knowledge_gaps import start_dingtalk_refresh
        start_dingtalk_refresh(asyncio.get_running_loop(), source)
    return source


@router.put("/knowledge-sources/{source_id}", response_model=KnowledgeSourceOut,
            dependencies=[Depends(require_role("super_admin", "admin"))])
async def update_knowledge_source(
    source_id: int,
    payload: KnowledgeSourceUpdate,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """更新知识库登记。"""
    source = await s.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(404, "知识库不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, key, value)
    await s.commit()
    await s.refresh(source)
    return source


@router.delete("/knowledge-sources/{source_id}",
               dependencies=[Depends(require_role("super_admin", "admin"))])
async def delete_knowledge_source(
    source_id: int,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """删除知识库登记。"""
    source = await s.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(404, "知识库不存在")
    await s.delete(source)
    await s.commit()
    return {"ok": True}


@router.get("/knowledge-sources/{source_id}/directories")
async def get_knowledge_source_directories(
    source_id: int,
    parent_node_id: str | None = Query(None, description="父节点 ID，不传则从知识库根目录开始"),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """获取某知识库下的目录（供其它模块通过知识库 ID 获取目录结构）。

    目前支持：
    - dingtalk_workspace：调用钉钉开放平台列出子节点
    - dify_dataset：返回 Dify 数据集内文档（简化为平铺列表）
    - business_system：返回空列表（待实现）
    """
    source = await s.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(404, "知识库不存在")
    if not source.enabled:
        raise HTTPException(400, "该知识库已停用")

    if source.source_type == "dingtalk_workspace":
        from kb_common.clients import dingtalk_client
        try:
            await dingtalk_client.sync_runtime_config()
            if not dingtalk_client.is_configured():
                raise HTTPException(400, "钉钉配置未就绪")
            # 优先用 config.root_node_id，否则用 external_id 作为 workspace 根
            root = (source.config or {}).get("root_node_id") or source.external_id
            nodes = await dingtalk_client.list_nodes(root)
            return {
                "source_type": source.source_type,
                "root_node_id": root,
                "items": [
                    {
                        "node_id": n.get("nodeId"),
                        "name": n.get("name") or "",
                        "is_folder": n.get("type") == "FOLDER",
                        "has_children": bool(n.get("hasChildren")),
                        "extension": n.get("extension"),
                        "url": n.get("url"),
                    }
                    for n in nodes
                ],
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"获取钉钉目录失败：{e}")

    if source.source_type == "dify_dataset":
        # Dify 数据集本身就是一层文档集合，无多级目录
        return {"source_type": source.source_type, "root_node_id": source.external_id, "items": []}

    if source.source_type == "ragflow_dataset":
        # RAGFlow 数据集：实时列出库内文档（含解析状态），比 Dify 分支更有用
        from kb_common.clients import ragflow_client
        from kb_common.config import get_settings as _gs
        from kb_common.models import Setting as _Setting
        # 应用运行时连接配置（settings 表优先）
        for _k in ("ragflow_base_url", "ragflow_api_key"):
            _row = (await s.execute(select(_Setting).where(_Setting.key == _k))).scalar_one_or_none()
            setattr(_gs(), _k, (_row.value if _row else None) or getattr(_gs(), _k, "") or "")
        try:
            docs = await ragflow_client.list_documents(source.external_id)
        except ragflow_client.RagflowNotConfigured as e:
            raise HTTPException(400, str(e))
        except ragflow_client.RagflowError as e:
            raise HTTPException(502, f"获取 RAGFlow 文档失败：{e}")
        return {
            "source_type": source.source_type,
            "root_node_id": source.external_id,
            "items": [
                {
                    "node_id": d.get("id"),
                    "name": d.get("name") or "",
                    "is_folder": False,
                    "has_children": False,
                    "extension": (d.get("name") or "").rsplit(".", 1)[-1].lower() if "." in (d.get("name") or "") else "",
                    "run_status": d.get("run"),
                    "chunk_count": d.get("chunk_count") or 0,
                }
                for d in docs
            ],
        }

    # business_system：暂未实现，返回空
    return {"source_type": source.source_type, "root_node_id": source.external_id, "items": []}


@router.get("/knowledge-sources/{source_id}/dingtalk-folder-snapshot")
async def get_dingtalk_folder_snapshot(
    source_id: int,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """读取钉钉知识库的文件夹快照（dingtalk_folder_stats 表），供目录选择等场景秒开。

    只读持久化快照，不实时调用钉钉；快照由知识源登记/知识缺口页「刷新」后台遍历写入。
    """
    source = await s.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(404, "知识库不存在")
    if source.source_type != "dingtalk_workspace":
        raise HTTPException(400, "仅钉钉知识库提供目录快照")
    rows = (await s.execute(
        select(DingtalkFolderStat)
        .where(DingtalkFolderStat.external_id == source.external_id)
        .order_by(DingtalkFolderStat.path, DingtalkFolderStat.node_id)
    )).scalars().all()
    return {
        "external_id": source.external_id,
        "fetched_at": _to_utc_iso(max((r.fetched_at for r in rows), default=None)),
        # folders：文件夹清单（path 不带前导斜杠，根为空串）；count=0 表示尚无快照，调用方可回退实时接口
        "count": len(rows),
        "folders": [{"node_id": r.node_id, "path": r.path} for r in rows],
    }


# ========== 知识中心 · 知识源文档（跨文档库聚合）==========

_DT_NODE_URL = "https://alidocs.dingtalk.com/i/nodes/{node_id}"


async def _dingtalk_fallback(s: AsyncSession, doc_ids: list[str]) -> dict[str, dict]:
    """存量钉钉文档缺失固化来源信息时的兜底：SyncDocumentMapping 反查 node_id，
    再经 SyncSource → KnowledgeSource 解析知识库名称（仅响应补全，不写库）。"""
    mappings = (await s.execute(
        select(SyncDocumentMapping)
        .where(SyncDocumentMapping.dify_document_id.in_(doc_ids)))).scalars().all()
    if not mappings:
        return {}
    src_rows = (await s.execute(
        select(SyncSource).where(SyncSource.id.in_({m.source_id for m in mappings})))).scalars().all()
    ws_ids = {sr.workspace_id for sr in src_rows if sr.workspace_id}
    ws_names: dict[str, str] = {}
    if ws_ids:
        ks_rows = (await s.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.source_type == "dingtalk_workspace",
                KnowledgeSource.external_id.in_(ws_ids)))).scalars().all()
        ws_names = {ks.external_id: ks.name for ks in ks_rows}
    out: dict[str, dict] = {}
    for m in mappings:
        if not m.node_id:
            continue
        out[m.dify_document_id] = {
            "source_url": _DT_NODE_URL.format(node_id=m.node_id),
            "source_workspace_name": ws_names.get(
                next((sr.workspace_id for sr in src_rows if sr.id == m.source_id), ""), None),
        }
    return out


def _source_doc_out(doc: LibraryDocument, library_name: str, extra: dict | None = None) -> dict:
    extra = extra or {}
    return {
        "id": str(doc.id), "name": doc.name, "tags": doc.tags or [],
        "source": doc.source, "status": doc.status, "size": doc.size,
        "library_id": doc.library_id, "library_name": library_name,
        "updated_by": doc.updated_by, "expire_at": _to_utc_iso(doc.expire_at),
        "updated_at": _to_utc_iso(doc.updated_at),
        "source_url": (doc.source_url if doc.source == "dingtalk" else None) or extra.get("source_url"),
        "source_workspace_name": (doc.source_workspace_name if doc.source == "dingtalk" else None)
        or extra.get("source_workspace_name"),
    }


@router.get("/source-documents/tags")
async def list_source_document_tags(
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """知识中心标签筛选选项：文档库全部标签去重计数（unnest 聚合，按使用数降序）。"""
    sub = (select(func.unnest(LibraryDocument.tags).label("tag"))
           .join(KnowledgeLibrary, LibraryDocument.library_id == KnowledgeLibrary.id)
           .where(KnowledgeLibrary.library_type == "document")).subquery()
    rows = (await s.execute(
        select(sub.c.tag.label("tag"), func.count().label("count"))
        .group_by(sub.c.tag).order_by(func.count().desc(), sub.c.tag))).all()
    return {"items": [{"name": r.tag, "count": r.count} for r in rows]}


@router.get("/source-documents")
async def list_source_documents(
    search: str | None = Query(None, description="按知识标题模糊搜索"),
    source: str | None = Query(None, description="来源筛选：local | dingtalk"),
    tag: str | None = Query(None, description="标签筛选（命中任一标签即返回）"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """知识中心文档列表：跨文档库聚合所有来源的知识源文档（原件存于 MinIO raw-docs）。

    钉钉来源（source=dingtalk）返回固化入库的在线文档链接与知识库名称
    （source_url / source_workspace_name）；迁移 0051 之前的存量文档由
    SyncDocumentMapping 反查兜底。本接口只读元数据库，不调钉钉。
    """
    q = (select(LibraryDocument, KnowledgeLibrary.name.label("library_name"))
         .join(KnowledgeLibrary, LibraryDocument.library_id == KnowledgeLibrary.id)
         .where(KnowledgeLibrary.library_type == "document"))
    if source in ("local", "dingtalk"):
        q = q.where(LibraryDocument.source == source)
    if tag and tag.strip():
        q = q.where(LibraryDocument.tags.any(tag.strip()))
    if search and search.strip():
        q = q.where(LibraryDocument.name.ilike(f"%{search.strip()}%"))
    total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await s.execute(
        q.order_by(LibraryDocument.updated_at.desc(), LibraryDocument.created_at.desc())
        .offset((page - 1) * size).limit(size))).all()

    fallback = await _dingtalk_fallback(
        s, [str(doc.id) for doc, _ in rows
            if doc.source == "dingtalk" and (not doc.source_url or not doc.source_workspace_name)])
    return {"items": [_source_doc_out(doc, lib_name,
                                      fallback.get(str(doc.id)) if doc.source == "dingtalk" else None)
                      for doc, lib_name in rows],
            "total": total}


class SourceDocUpdateIn(BaseModel):
    """知识中心文档编辑：标签（与库内 DocumentTagsIn 同口径校验）+ 过期时间。"""
    tags: list[str] = Field(default_factory=list, max_length=32)
    expire_at: str | None = Field(None, description="过期时间（ISO 日期），空串或 null 表示清除")

    @field_validator("tags")
    @classmethod
    def _trim(cls, v):
        cleaned = [t.strip() for t in v if t and t.strip()]
        seen, out = set(), []
        for t in cleaned:
            short = t[:64]
            if short not in seen:
                seen.add(short)
                out.append(short)
        if len(out) > 32:
            raise ValueError("标签最多 32 个")
        return out


@router.put("/source-documents/{doc_id}",
            dependencies=[Depends(require_role("super_admin", "admin", "editor"))])
async def update_source_document(
    doc_id: _uuid.UUID,
    body: SourceDocUpdateIn,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """编辑知识中心文档元数据：标签与过期时间（更新人记录为当前登录用户）。"""
    doc = await s.get(LibraryDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    doc.tags = body.tags
    raw_expire = (body.expire_at or "").strip()
    if raw_expire:
        try:
            doc.expire_at = datetime.fromisoformat(raw_expire)
        except ValueError:
            raise HTTPException(422, "过期时间格式无效，应为日期或日期时间")
    else:
        doc.expire_at = None
    doc.updated_by = u.username
    await s.commit()
    await s.refresh(doc)
    lib = await s.get(KnowledgeLibrary, doc.library_id)
    return _source_doc_out(doc, lib.name if lib else "")
