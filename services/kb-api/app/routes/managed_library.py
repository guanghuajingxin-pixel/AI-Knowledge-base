"""Project document libraries: MinerU parsing + fully local chunking.

解析链路：MinIO 原件 → MinerU（mineru-kit V1）异步解析 job → 前端轮询 refresh；
job 完成时下载 markdown → 本地分段器按库分段规则切块 → LibraryChunk 落库。
分段 CRUD / 导出 / 检索测试全部本地化，不再依赖 RAGFlow。
"""
import asyncio
import re
import uuid
import zipfile
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, func, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import require_role, get_current_user
from app.services.knowledge_engines import EngineError, MinerUEngine, MinerUCloudEngine, job_state
from app.services.knowledge_engines.local_chunker import chunk_markdown
from kb_common.config import get_settings
from kb_common.database import get_session
from kb_common.models import (EmbeddingProfile, KnowledgeLibrary, LibraryChunk,
                              LibraryDocument, RerankProfile, Setting, User)

router = APIRouter(prefix="/api/v1/document-libraries", tags=["document-libraries"],
    dependencies=[Depends(require_role("super_admin", "admin", "editor"))])

# 图片代理独立路由：不带 router 级 Bearer 依赖（<img> 请求无头），由端点内 ?token= 校验
image_router = APIRouter(prefix="/api/v1/document-libraries", tags=["document-library-images"])


class ProcessingConfig(BaseModel):
    chunk_method: Literal["auto", "naive", "book", "laws", "manual", "one", "paper", "presentation", "table"] = "naive"
    layout_recognize: Literal["DeepDOC", "Plain Text"] = "DeepDOC"
    chunk_token_num: int = Field(512, ge=1, le=2048)
    delimiter: str = Field("\n。！？；", min_length=1, max_length=100)
    embedding_model: str = Field("", max_length=200)
    # 分段重叠度：滚动聚合时相邻分段重复携带的 Token 数（0 = 不重叠）
    overlap: int = Field(25, ge=0, le=1024)
    # 文本预处理规则（分段前执行，语义与 Dify pre_processing_rules 对齐）
    replace_whitespace: bool = False
    remove_urls_emails: bool = False

    enable_children: bool = False
    children_delimiter: str = Field("\n", min_length=1, max_length=100)
    # 父子分段增强：父块模式（paragraph=分段作父块 / fulltext=整篇作父块，超 10000 Token 截断）
    parent_mode: Literal["paragraph", "fulltext"] = "paragraph"
    # 父子分段：子块最大长度——按子分隔符切出后超长子块在句界再细切
    children_chunk_token_num: int = Field(200, ge=1, le=2048)
    auto_keywords: int = Field(0, ge=0, le=32)
    auto_questions: int = Field(0, ge=0, le=10)

    @model_validator(mode="after")
    def validate_children(self):
        if self.enable_children and self.chunk_method != "naive":
            raise ValueError("父子分段当前仅支持通用文档策略")
        return self


class RetrievalConfig(BaseModel):
    """库级检索设置（知识库设置-检索设置）：保存后立即生效。

    检索测试未显式传参时按此取库级默认；mode=hybrid 时 rerank 与权重设置互斥
    （rerank=False 即权重设置子策略，vector_weight 为语义权重，关键词权重=1-该值）。
    """
    mode: Literal["hybrid", "vector", "fulltext"] = "hybrid"
    vector_weight: float = Field(0.7, ge=0, le=1)
    rerank: bool = False
    rerank_model_id: str = Field("", max_length=64)
    top_k: int = Field(8, ge=1, le=50)
    score_threshold: float = Field(0.0, ge=0, le=1)


class LibraryIn(ProcessingConfig):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    # 索引设置策略视图（与文档级 DocumentConfigIn 对齐）：processing 为解析生效的最终分段参数
    strategy: Literal["auto", "custom", "parent_child", "by_file_type"] = "auto"
    enhancements: dict[str, bool] = Field(default_factory=dict)
    type_rules: dict = Field(default_factory=dict)
    # 库级检索设置（仅知识库级；文档级不单独设置检索参数）
    retrieval: RetrievalConfig | None = None

    @field_validator("name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("知识库名称不能为空")
        return value.strip()


class ChunkIn(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    available: bool = True
    important_keywords: list[str] = Field(default_factory=list, max_length=32)
    # 新增分段时的插入位置：二选一，值为目标分段 ID（缺省追加到末尾）
    insert_before: str | None = Field(default=None, max_length=64)
    insert_after: str | None = Field(default=None, max_length=64)

    @field_validator("content")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("分段内容不能为空")
        return value

    @model_validator(mode="after")
    def validate_insert_target(self):
        if self.insert_before and self.insert_after:
            raise ValueError("向前插入与向后插入只能选择其一")
        return self


class DocumentEnabledIn(BaseModel):
    enabled: bool


class StagingDiscardIn(BaseModel):
    """丢弃暂存文件：staging_id 列表（UUID，防路径注入）。"""
    staging_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


class StagedFileIn(BaseModel):
    """确认入库载荷项：暂存上传返回的原样回传（staging_id 定位暂存对象）。"""
    staging_id: uuid.UUID
    name: str = Field(min_length=1, max_length=500)
    size: int = Field(ge=0)


class StagingCommitIn(BaseModel):
    items: list[StagedFileIn] = Field(min_length=1, max_length=50)


class DocumentConfigIn(BaseModel):
    """文档级索引设置：strategy 为前端策略视图，processing 为解析生效的最终分段参数。"""
    processing: ProcessingConfig
    strategy: Literal["auto", "custom", "parent_child", "by_file_type"] = "auto"
    enhancements: dict[str, bool] = Field(default_factory=dict)
    type_rules: dict = Field(default_factory=dict)


async def library(s, library_id, lock=False):
    q = select(KnowledgeLibrary).where(KnowledgeLibrary.id == library_id, KnowledgeLibrary.library_type == "document")
    if lock:
        q = q.with_for_update()
    lib = (await s.execute(q)).scalar_one_or_none()
    if not lib:
        raise HTTPException(404, "文档库不存在")
    return lib


def engine() -> "MinerUEngine | MinerUCloudEngine":
    """文档库解析引擎选择：

    - 显式配置 STRUCTURED_KIT_BASE_URL（非空）→ 本地 mineru-kit V1；
    - 留空 → 云端 SaaS（复用 mineru_saas，Key 取 settings 表 > .env）。

    生产（10.10.166.2 等）未部署本地 kit，靠留空 STRUCTURED_KIT_BASE_URL 走云，
    与 mineru_route 的 DEFAULT_BASE=cloud、「模型配置」页 MinerU Key 一致。
    """
    base = (get_settings().structured_kit_base_url or "").strip()
    if base:
        try:
            return MinerUEngine(base)
        except EngineError as exc:
            raise HTTPException(503, str(exc)) from exc
    return MinerUCloudEngine()


async def invoke(call):
    try:
        return await call
    except EngineError as exc:
        raise HTTPException(502, str(exc)) from exc


def _library_engine_config(body: LibraryIn) -> dict:
    """库级 engine_config：processing（解析生效参数）+ 策略视图 + 检索设置。"""
    return {"processing": body.model_dump(exclude={"name", "description", "strategy", "enhancements", "type_rules", "retrieval"}),
            "strategy": body.strategy, "enhancements": body.enhancements, "type_rules": body.type_rules,
            "retrieval": body.retrieval.model_dump() if body.retrieval else {}}


async def _validate_retrieval_model(s: AsyncSession, body: LibraryIn) -> None:
    """检索设置中的 Rerank 模型必须是「模型配置」里已生效的模型；空值=用全局生效配置。"""
    r = body.retrieval
    if not (r and r.rerank and r.rerank_model_id.strip()):
        return
    try:
        rid = uuid.UUID(r.rerank_model_id.strip())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "所选 Rerank 模型无效")
    row = await s.get(RerankProfile, rid)
    if row is None or not row.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "所选 Rerank 模型未生效，请先在「模型配置」中启用后再选择")


def _library_config(lib) -> dict:
    """库级配置输出：与文档级 {processing, strategy, enhancements, type_rules} 结构对齐
    （外加库级 retrieval 检索设置）；旧库未存 strategy 时按 processing 推导
    （父子 → parent_child，auto → auto，否则 custom）。"""
    cfg = lib.engine_config or {}
    processing = cfg.get("processing") or {}
    strategy = cfg.get("strategy") or (
        "parent_child" if processing.get("enable_children")
        else "auto" if processing.get("chunk_method") == "auto" else "custom")
    return {"processing": processing, "strategy": strategy,
            "enhancements": cfg.get("enhancements") or {}, "type_rules": cfg.get("type_rules") or {},
            "retrieval": cfg.get("retrieval") or {}}


def lib_out(lib, count=0):
    return {"id": lib.id, "name": lib.name, "description": lib.description,
            "enabled": lib.enabled, "document_count": count, "creator": lib.creator or "",
            "config": _library_config(lib), "created_at": lib.created_at}


def doc_out(doc):
    return {key: getattr(doc, key) for key in ("id", "name", "size", "status", "progress", "message", "chunk_count", "enabled", "source", "source_url", "source_workspace_name", "updated_by", "expire_at", "parsed_at", "tags", "created_at", "updated_at")} | \
           {"config": doc.engine_config or {}}


def chunk_out(c: LibraryChunk) -> dict:
    return {"id": str(c.id), "document_id": str(c.document_id), "content": c.content,
            "available": c.available, "important_keywords": c.important_keywords or [],
            "position": c.position, "parent_id": str(c.parent_id) if c.parent_id else None}


async def parent_chunk_count(s, doc_id) -> int:
    return (await s.execute(select(func.count()).select_from(LibraryChunk).where(
        LibraryChunk.document_id == doc_id, LibraryChunk.parent_id.is_(None)))).scalar_one()


@router.get("")
async def list_libraries(s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(select(KnowledgeLibrary, func.count(LibraryDocument.id))
        .outerjoin(LibraryDocument, LibraryDocument.library_id == KnowledgeLibrary.id)
        .where(KnowledgeLibrary.library_type == "document")
        .group_by(KnowledgeLibrary.id).order_by(KnowledgeLibrary.id.desc()))).all()
    return [lib_out(lib, count) for lib, count in rows]


@router.post("")
async def create_library(body: LibraryIn, user: User = Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    await _validate_retrieval_model(s, body)
    lib = KnowledgeLibrary(name=body.name, description=body.description, platform="mineru",
        dataset_id=uuid.uuid4().hex, library_type="document", enabled=True, creator=user.username,
        engine_config=_library_engine_config(body))
    s.add(lib)
    await s.commit()
    await s.refresh(lib)
    return lib_out(lib)


@router.get("/embedding-models")
async def embedding_models(s: AsyncSession = Depends(get_session)):
    """向量模型候选：Embedding 多配置生效条 → settings 单值回退。

    分段与检索已本地化，向量模型仅作为库配置元数据保留（供后续向量化接入）。
    """
    row = (await s.execute(select(EmbeddingProfile).where(
        EmbeddingProfile.enabled == True))).scalars().first()  # noqa: E712
    model = (row.model or "").strip() if row else ""
    if not model:
        setting = (await s.execute(select(Setting).where(Setting.key == "embedding_model"))).scalar_one_or_none()
        model = (setting.value or "").strip() if setting else ""
    if not model:
        model = get_settings().embedding_model or "bge-m3"
    return [{"id": model, "name": model}]


@router.put("/{library_id}")
async def configure_library(library_id: int, body: LibraryIn, s: AsyncSession = Depends(get_session)):
    await _validate_retrieval_model(s, body)
    lib = await library(s, library_id, lock=True)
    lib.name, lib.description = body.name, body.description
    lib.engine_config = _library_engine_config(body)
    await s.commit()
    await s.refresh(lib)
    return lib_out(lib)


@router.get("/{library_id}/documents")
async def list_documents(library_id: int, s: AsyncSession = Depends(get_session)):
    await library(s, library_id)
    docs = (await s.execute(select(LibraryDocument).where(LibraryDocument.library_id == library_id)
        .order_by(LibraryDocument.created_at.desc()))).scalars().all()
    return [doc_out(doc) for doc in docs]


_ALLOWED_UPLOAD_EXTS = {"pdf", "docx", "txt", "md", "csv", "xlsx", "pptx", "html"}
_MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def _validated_upload_name(filename: str | None) -> str:
    """上传文件名校验：取 basename、限长、限扩展名（暂存上传与确认入库共用）。"""
    name = (filename or "").replace("\\", "/").split("/")[-1]
    if not name or len(name) > 500:
        raise HTTPException(422, "文件名为空或超过 500 字符")
    if name.rsplit(".", 1)[-1].lower() not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(422, "不支持的文件类型")
    return name


def _staging_prefix(library_id: int, staging_id) -> str:
    return f"document-libraries/{library_id}/staging/{staging_id}/"


@router.post("/{library_id}/documents")
async def upload(library_id: int, file: UploadFile = File(...),
                 user: User = Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    await library(s, library_id)
    name = _validated_upload_name(file.filename)
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    if not content or len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "请上传非空且不超过 64 MB 的文件")
    from kb_common.clients import minio_client
    doc_id = uuid.uuid4()
    key = f"document-libraries/{library_id}/{doc_id}/{name}"
    await asyncio.to_thread(minio_client.upload_bytes, minio_client.RAW, key, content)
    doc = LibraryDocument(id=doc_id, library_id=library_id, name=name, storage_path=key, size=len(content),
                          status="UPLOADED", progress=0, message="", chunk_count=0,
                          updated_by=user.username)
    s.add(doc)
    await s.commit()
    await s.refresh(doc)
    # Auto-parse synchronously: upload to MinerU + start parse job.
    # Most documents finish engine-side upload within a few seconds; the user
    # sees "解析中" immediately and the frontend polls for completion.
    try:
        await _do_parse(s, library_id, doc_id)
        await s.refresh(doc)
    except HTTPException:
        raise
    except Exception as exc:
        doc.status, doc.message = "FAILED", f"自动解析失败，请重新解析"
        await s.commit()
        await s.refresh(doc)
    return doc_out(doc)


@router.post("/{library_id}/staging")
async def stage_upload(library_id: int, file: UploadFile = File(...), s: AsyncSession = Depends(get_session)):
    """本地文件暂存上传：仅落 MinIO 暂存区，不建文档记录、不触发解析。

    「添加知识」弹窗选中文件即调用；点确定走 /documents/commit 入库并自动解析，
    取消/关闭弹窗走 /staging/discard 清理暂存对象。
    """
    await library(s, library_id)
    name = _validated_upload_name(file.filename)
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    if not content or len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "请上传非空且不超过 64 MB 的文件")
    from kb_common.clients import minio_client
    staging_id = uuid.uuid4()
    key = f"{_staging_prefix(library_id, staging_id)}{name}"
    await asyncio.to_thread(minio_client.upload_bytes, minio_client.RAW, key, content)
    return {"staging_id": str(staging_id), "name": name, "size": len(content)}


@router.post("/{library_id}/staging/discard")
async def staging_discard(library_id: int, body: StagingDiscardIn, s: AsyncSession = Depends(get_session)):
    """丢弃暂存文件：按 staging_id 删除 MinIO 暂存区对象（幂等，对象不存在不报错）。"""
    await library(s, library_id)
    from kb_common.clients import minio_client

    def remove_all():
        for sid in body.staging_ids:
            minio_client.delete_prefix(minio_client.RAW, _staging_prefix(library_id, sid))
    await asyncio.to_thread(remove_all)
    return {"ok": True, "discarded": len(body.staging_ids)}


@router.post("/{library_id}/documents/commit")
async def staging_commit(library_id: int, body: StagingCommitIn,
                         user: User = Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    """确认暂存文件入库：暂存对象迁移到正式路径 → 建文档记录 → 自动解析。

    逐项处理互不阻断；迁移/入库失败的项目清理其暂存对象并记入 errors，
    解析失败沿用单文件上传语义（文档置 FAILED，可手动重新解析）。
    """
    await library(s, library_id)
    from kb_common.clients import minio_client
    results, errors = [], []
    for item in body.items:
        try:
            name = _validated_upload_name(item.name)
        except HTTPException as exc:
            errors.append(f"{item.name}: {exc.detail}")
            continue
        staging_prefix = _staging_prefix(library_id, item.staging_id)
        doc_id = uuid.uuid4()
        key = f"document-libraries/{library_id}/{doc_id}/{name}"
        try:
            def migrate():
                minio_client.copy_object(minio_client.RAW, f"{staging_prefix}{name}", minio_client.RAW, key)
                minio_client.delete_prefix(minio_client.RAW, staging_prefix)
            await asyncio.to_thread(migrate)
            doc = LibraryDocument(id=doc_id, library_id=library_id, name=name, storage_path=key,
                                  size=item.size, status="UPLOADED", progress=0, message="", chunk_count=0,
                                  updated_by=user.username)
            s.add(doc)
            await s.commit()
            await s.refresh(doc)
            try:
                await _do_parse(s, library_id, doc_id)
                await s.refresh(doc)
            except Exception:
                doc.status, doc.message = "FAILED", "自动解析失败，请重新解析"
                await s.commit()
                await s.refresh(doc)
            results.append(doc_out(doc))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            try:
                await asyncio.to_thread(minio_client.delete_prefix, minio_client.RAW, staging_prefix)
            except Exception:
                pass  # 清理失败容忍：暂存前缀隔离，不影响正式数据
    return {"documents": results, "errors": errors}


class DingTalkImportIn(BaseModel):
    """钉钉导入请求体：指定文档节点列表 或 整个知识库。"""
    node_ids: list[str] = Field(default_factory=list, description="钉钉文档节点 ID 列表")
    workspace_id: str = Field("", description="钉钉知识库 ID（整库导入时必填）")
    root_node_id: str = Field("", description="知识库根节点 ID（整库导入时必填）")
    import_mode: Literal["once", "sync"] = Field("once", description="once=一次性导入, sync=自动同步")


@router.post("/{library_id}/import-dingtalk")
async def import_dingtalk(library_id: int, body: DingTalkImportIn,
                          user: User = Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    """从钉钉知识库导入文档到文档库。

    支持两种模式：
    - 指定文档：传 node_ids 列表
    - 整库导入：传 workspace_id + root_node_id，递归遍历所有文档
    """
    await library(s, library_id)

    if not body.node_ids and not body.root_node_id:
        raise HTTPException(422, "请指定钉钉文档节点或知识库")

    # 获取钉钉客户端
    from app.services.sync.sync_database import SyncSessionLocal
    from app.services.sync.sync_settings import make_dingtalk_client
    from app.services.sync.source_files import fetch_source_file
    from kb_common.clients import minio_client
    import tempfile
    from pathlib import Path

    with SyncSessionLocal() as sync_db:
        dt = make_dingtalk_client(sync_db)

    try:
        # 知识库名称映射（workspaceId → 名称）：固化到文档行，供知识中心展示来源系统
        try:
            ws_names = {ws.get("workspaceId"): ws.get("name", "")
                        for ws in dt.list_workspaces() if ws.get("workspaceId")}
        except Exception:
            ws_names = {}

        def _dt_source(node: dict) -> dict:
            """钉钉来源固化信息：在线文档链接 + 知识库名称。"""
            node_id = node.get("nodeId") or node.get("node_id", "")
            ws_id = node.get("workspaceId") or node.get("workspace_id", "") or body.workspace_id
            return {"source_url": f"https://alidocs.dingtalk.com/i/nodes/{node_id}" if node_id else None,
                    "source_workspace_name": ws_names.get(ws_id) or None}

        # 确定要导入的节点列表
        nodes_to_import: list[dict] = []
        if body.node_ids:
            # 指定文档模式：逐个获取节点信息
            for nid in body.node_ids:
                try:
                    resp = dt.get_node(nid)
                    node = resp.get("node") or resp
                    node["nodeId"] = nid
                    nodes_to_import.append(node)
                except Exception as e:
                    raise HTTPException(400, f"获取钉钉节点 {nid} 失败：{e}")
        else:
            # 整库模式：递归遍历
            from kb_common.config import get_settings as gs
            settings = gs()
            nodes_to_import = dt.walk_tree(body.root_node_id,
                                           settings.sync_max_depth,
                                           settings.sync_max_results_per_page,
                                           use_cache=False)

        if not nodes_to_import:
            raise HTTPException(400, "未找到可导入的钉钉文档")

        # 逐个下载并创建文档
        results = []
        errors = []
        with tempfile.TemporaryDirectory(prefix="dingtalk-import-") as tmp:
            tmp_path = Path(tmp)
            for node in nodes_to_import:
                node_id = node.get("nodeId") or node.get("node_id", "")
                name = node.get("name", node_id)
                try:
                    # 下载/导出钉钉文件
                    local_file = fetch_source_file(dt, node, tmp_path)
                    content = local_file.read_bytes()
                    file_name = local_file.name

                    # 检查文件类型是否支持
                    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
                    if ext not in {"pdf", "docx", "txt", "md", "csv", "xlsx", "pptx", "html"}:
                        errors.append(f"{name}: 不支持的文件类型 .{ext}")
                        continue

                    # 上传到 MinIO
                    doc_id = uuid.uuid4()
                    key = f"document-libraries/{library_id}/{doc_id}/{file_name}"
                    await asyncio.to_thread(minio_client.upload_bytes, minio_client.RAW, key, content)

                    # 创建文档记录（source=dingtalk：钉钉知识库同步来源，与本地上传区分；
                    # 固化钉钉链接与知识库名称，更新人=导入操作者）
                    doc = LibraryDocument(id=doc_id, library_id=library_id, name=file_name,
                                          storage_path=key, size=len(content), source="dingtalk",
                                          status="UPLOADED", progress=0, message="", chunk_count=0,
                                          updated_by=user.username, **_dt_source(node))
                    s.add(doc)
                    await s.commit()
                    await s.refresh(doc)

                    # 触发解析
                    try:
                        await _do_parse(s, library_id, doc_id)
                        await s.refresh(doc)
                    except Exception:
                        doc.status, doc.message = "FAILED", "自动解析失败，请重新解析"
                        await s.commit()
                        await s.refresh(doc)

                    results.append(doc_out(doc))
                except Exception as e:
                    errors.append(f"{name}: {e}")

        return {
            "imported": len(results),
            "failed": len(errors),
            "errors": errors[:10],  # 最多返回 10 条错误
            "documents": results,
        }
    finally:
        dt.close()


async def bound_document(s, library_id, doc_id, lock=False):
    lib = await library(s, library_id, lock=lock)
    q = select(LibraryDocument).where(LibraryDocument.id == doc_id, LibraryDocument.library_id == library_id)
    if lock:
        q = q.with_for_update()
    doc = (await s.execute(q)).scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "文档不存在")
    return lib, doc


# 「按文件类型」未命中/自动规则的回落配置（与前端 auto 策略口径一致）
_AUTO_PROCESSING = {"chunk_method": "auto", "chunk_token_num": 512, "delimiter": "\n。！？；",
                    "overlap": 25, "enable_children": False}


def _type_rule_processing(rule: dict | None, base: dict) -> dict:
    """「按文件类型」规则 → 生效 processing：未命中/auto 规则回落自动；
    custom/parent_child 规则在库级基础上覆盖方法/长度/分隔符等关键键。"""
    if not rule or rule.get("strategy") == "auto":
        return {**base, **_AUTO_PROCESSING}
    merged = {**base, "enable_children": False}
    if rule.get("delimiter"):
        merged["delimiter"] = rule["delimiter"]
    if rule.get("chunk_token_num"):
        merged["chunk_token_num"] = int(rule["chunk_token_num"])
    if rule.get("children_delimiter"):
        merged["children_delimiter"] = rule["children_delimiter"]
    if rule.get("strategy") == "parent_child":
        merged["chunk_method"] = "naive"
        merged["enable_children"] = True
    else:  # custom：规则指定的分段方式
        merged["chunk_method"] = rule.get("method") or "naive"
    return merged


def _processing_for(lib, doc) -> dict:
    """分段生效配置：文档级 engine_config.processing 逐键覆盖库级（空值除外，避免清空库配置），
    缺省回退库级；库级「按文件类型」策略按文档扩展名路由 type_rules（未命中回落自动），
    文档级单独设置过策略时不路由（文档设置弹窗不展示按文件类型）。"""
    lib_cfg = lib.engine_config or {}
    doc_cfg = doc.engine_config or {}
    merged = dict(lib_cfg.get("processing") or {})
    override = dict(doc_cfg.get("processing") or {})
    merged.update({k: v for k, v in override.items() if v not in ("", None)})
    if not doc_cfg.get("strategy") and lib_cfg.get("strategy") == "by_file_type":
        ext = doc.name.rsplit(".", 1)[-1].lower() if "." in doc.name else ""
        merged = _type_rule_processing((lib_cfg.get("type_rules") or {}).get(ext), merged)
    return merged


def _job_message(job: dict) -> str:
    for f in job.get("files") or []:
        if isinstance(f, dict) and f.get("error"):
            return str(f["error"])
    return str(job.get("error") or "")


async def _apply_chunks(s: AsyncSession, doc, markdown: str, processing: dict, partial: bool = False):
    """按库分段规则把 markdown 落地为 LibraryChunk（覆盖旧分段）。"""
    pieces = chunk_markdown(markdown, processing)
    if not pieces:
        doc.status, doc.progress, doc.message = "FAILED", 0, "解析产物为空，请检查文件内容"
        return
    await s.execute(delete(LibraryChunk).where(LibraryChunk.document_id == doc.id))
    count = 0
    for position, piece in enumerate(pieces):
        parent = LibraryChunk(document_id=doc.id, content=piece["content"], available=True,
                              important_keywords=[], position=position)
        s.add(parent)
        count += 1
        if piece["children"]:
            await s.flush()  # 需要 parent.id 建立父子关系
            for child_index, text in enumerate(piece["children"]):
                s.add(LibraryChunk(document_id=doc.id, parent_id=parent.id, content=text,
                                   available=True, important_keywords=[], position=position,
                                   child_index=child_index))
    doc.status, doc.progress = "COMPLETED", 1.0
    doc.message = "部分内容解析失败，请检查分段" if partial else ""
    doc.chunk_count = count
    from datetime import datetime as _dt
    doc.parsed_at = _dt.utcnow()
    await _store_parsed_markdown(doc, markdown)


def _parsed_markdown_key(doc) -> str:
    """解析原文对象键：与原件同级的 parsed.md（txt/md/csv 原文即分段输入，同样落此键）。"""
    return f"{doc.storage_path.rsplit('/', 1)[0]}/parsed.md"


async def _store_parsed_markdown(doc, text: str):
    """解析原文落 MinIO，供分段页「查看解析原文」读取（重解析同名覆盖）。"""
    if not text:
        return
    from kb_common.clients import minio_client
    await asyncio.to_thread(minio_client.upload_bytes, minio_client.RAW,
                            _parsed_markdown_key(doc), text.encode("utf-8"), "text/markdown")


def _sniff_format(text: str) -> Literal["json", "markdown"]:
    """解析原文展示格式：合法 JSON 对象/数组按 JSON 视图，其余按 markdown 渲染。"""
    import json
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return "json" if isinstance(json.loads(stripped), (dict, list)) else "markdown"
        except ValueError:
            return "markdown"
    return "markdown"


_IMAGE_NAME_RE = re.compile(r"^[\w.-]+$")
_IMAGE_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}


def _extract_zip_bundle(data: bytes) -> tuple[str, dict[str, bytes]]:
    """解 MinerU zip 产物：markdown 正文 + images/ 图片字节。
    markdown 中的图片保持 MinerU 原始相对引用（images/xxx.jpg），由渲染层改写为代理 URL。"""
    import io
    import zipfile
    markdown = ""
    images: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            base = name.rsplit("/", 1)[-1]
            if not base or not _IMAGE_NAME_RE.match(base):
                continue
            if name.startswith("images/") and base.rsplit(".", 1)[-1].lower() in {
                    e.lstrip(".") for e in _IMAGE_MEDIA}:
                images[base] = zf.read(info)
            elif not markdown and base.endswith(".md"):
                markdown = zf.read(info).decode("utf-8", errors="replace")
    return markdown, images


def _images_prefix(doc) -> str:
    """图片在 MinIO 的目录：与原件同级的 images/ 子目录。"""
    return f"{doc.storage_path.rsplit('/', 1)[0]}/images"


async def _store_images(doc, images: dict[str, bytes]):
    """解析产物的图片落 MinIO，键：{原件目录}/images/{文件名}（重解析同名覆盖）。"""
    if not images:
        return
    from kb_common.clients import minio_client

    def upload_all():
        for name, data in images.items():
            ext = "." + name.rsplit(".", 1)[-1].lower()
            minio_client.upload_bytes(minio_client.RAW, f"{_images_prefix(doc)}/{name}",
                                      data, _IMAGE_MEDIA.get(ext, "application/octet-stream"))
    await asyncio.to_thread(upload_all)


async def _finalize_parse(adapter: "MinerUEngine | MinerUCloudEngine", s: AsyncSession, lib, doc, job: dict):
    """job 完成后的落地动作：下载产物（优先 zip，含图片）→ 图片落 MinIO → 按文档生效规则分段。"""
    try:
        markdown, images = _extract_zip_bundle(await invoke(adapter.zip_bundle(job)))
    except (EngineError, ValueError, zipfile.BadZipFile):
        markdown, images = "", {}
    if not markdown:
        # zip 不可用（旧引擎/产物缺失）时回退独立 markdown（base64 内联图会被清洗剔除）
        markdown = await invoke(adapter.markdown(job))
    await _store_images(doc, images)
    await _apply_chunks(s, doc, markdown, _processing_for(lib, doc),
                        partial=str(job.get("status", "")).lower() == "partial")


async def refresh_state(adapter: "MinerUEngine | MinerUCloudEngine", s: AsyncSession, lib, doc):
    """同步文档状态：查询 MinerU job；首次完成时执行本地分段（幂等，
    仅从 PARSING 状态迁出时落地，避免重复下载/重复分段）。"""
    if not doc.engine_job_id:
        # 旧 RAGFlow 时代的文档没有本地解析任务
        if doc.engine_document_id and doc.status == "PARSING":
            doc.status, doc.progress, doc.message = "FAILED", 0, "解析引擎已切换为 MinerU，请重新解析"
        return
    job = await invoke(adapter.job(doc.engine_job_id))
    status, progress = job_state(job)
    if status == "UNKNOWN":
        return
    if status == "PARSING":
        doc.status, doc.progress, doc.message = "PARSING", progress, str(job.get("status") or "")
        return
    if status == "COMPLETED" and doc.status == "PARSING":
        await _finalize_parse(adapter, s, lib, doc, job)
        return
    if status == "FAILED":
        doc.status, doc.progress = "FAILED", 0
        doc.message = _job_message(job) or "解析失败"
    elif status == "CANCELLED":
        doc.status, doc.progress, doc.message = "CANCELLED", 0, "已取消"


@router.post("/{library_id}/documents/{doc_id}/refresh")
async def refresh(library_id: int, doc_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    lib, doc = await bound_document(s, library_id, doc_id, lock=True)
    adapter = engine()
    await refresh_state(adapter, s, lib, doc)
    await s.commit()
    await s.refresh(doc)
    return doc_out(doc)


async def _do_parse(s: AsyncSession, library_id: int, doc_id: uuid.UUID):
    """Core parse logic: upload original to MinerU and start a parse job.

    Used both by the manual parse endpoint and the auto-parse call triggered
    right after upload. Caller is responsible for committing.
    """
    lib, doc = await bound_document(s, library_id, doc_id, lock=True)
    if doc.status == "PARSING":
        return
    adapter = engine()
    from kb_common.clients import minio_client
    def read_original():
        response = minio_client.minio.get_object(minio_client.RAW, doc.storage_path)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    raw = await asyncio.to_thread(read_original)
    ext = doc.name.rsplit(".", 1)[-1].lower() if "." in doc.name else ""
    if ext in {"txt", "md", "csv"}:
        # 纯文本类型：MinerU 不支持，原文即分段输入，直接本地分段
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        await _apply_chunks(s, doc, text, _processing_for(lib, doc))
        await s.commit()
        return
    file_id = await invoke(adapter.upload(doc.name, raw))
    job_id = await invoke(adapter.create_job(file_id))
    # 重新解析会清除旧分段（含人工修改），完成后按库当前规则重建
    await s.execute(delete(LibraryChunk).where(LibraryChunk.document_id == doc.id))
    doc.engine_document_id, doc.engine_job_id = file_id, job_id
    doc.status, doc.progress, doc.message, doc.chunk_count = "PARSING", 0, "已提交解析", 0
    await s.commit()


@image_router.get("/{library_id}/documents/{doc_id}/images/{image_name}")
async def serve_image(library_id: int, doc_id: uuid.UUID, image_name: str,
                      token: str = Query("", max_length=16384),
                      s: AsyncSession = Depends(get_session)):
    """分段内图片代理：<img> 无法携带 Authorization 头，JWT 改走 ?token= query。
    角色要求与文档库主路由一致（super_admin/admin/editor），不能挂在主 router 上
    （其 router 级 Bearer 依赖会拒绝无头请求）。"""
    from kb_common.oidc import authenticate
    user = await authenticate(token)
    if user.role not in ("super_admin", "admin", "editor"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")

    _, doc = await bound_document(s, library_id, doc_id)
    if not _IMAGE_NAME_RE.match(image_name):
        raise HTTPException(400, "非法文件名")
    key = f"{_images_prefix(doc)}/{image_name}"
    from kb_common.clients import minio_client

    def read():
        response = minio_client.minio.get_object(minio_client.RAW, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    try:
        data = await asyncio.to_thread(read)
    except Exception:
        raise HTTPException(404, "图片不存在")
    ext = "." + image_name.rsplit(".", 1)[-1].lower()
    return Response(data, media_type=_IMAGE_MEDIA.get(ext, "application/octet-stream"),
                    headers={"Cache-Control": "private, max-age=3600"})


@router.post("/{library_id}/documents/{doc_id}/parse")
async def parse(library_id: int, doc_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    lib, doc = await bound_document(s, library_id, doc_id, lock=True)
    if doc.status == "PARSING":
        raise HTTPException(409, "文档正在解析，请等待完成或停止后重试")
    if doc.status == "PENDING":
        raise HTTPException(409, "文档待同步，同步完成后可解析")
    await _do_parse(s, library_id, doc_id)
    await s.refresh(doc)
    return doc_out(doc)


@router.post("/{library_id}/documents/{doc_id}/stop")
async def stop(library_id: int, doc_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    lib, doc = await bound_document(s, library_id, doc_id, lock=True)
    if doc.status != "PARSING":
        raise HTTPException(409, "只有解析中的文档可以停止")
    if doc.engine_job_id:
        adapter = engine()
        await invoke(adapter.cancel(doc.engine_job_id))
    await s.execute(delete(LibraryChunk).where(LibraryChunk.document_id == doc.id))
    doc.status, doc.progress, doc.chunk_count, doc.message = "CANCELLED", 0, 0, "已停止"
    await s.commit()
    await s.refresh(doc)
    return doc_out(doc)


@router.post("/{library_id}/documents/{doc_id}/enabled")
async def set_document_enabled(library_id: int, doc_id: uuid.UUID, body: DocumentEnabledIn,
                               s: AsyncSession = Depends(get_session)):
    """文档级检索开关：禁用=分段从检索通道摘除；启用=恢复检索。

    分段数据保留在本地库中，启用即时生效（无需重新解析，人工修改的分段不丢）；
    等价于"删索引/重建索引"的检索语义。
    """
    _, doc = await bound_document(s, library_id, doc_id, lock=True)
    if doc.status == "PARSING":
        raise HTTPException(409, "解析中的文档不能更改检索状态，请先停止解析")
    doc.enabled = body.enabled
    await s.commit()
    await s.refresh(doc)
    return doc_out(doc)


class DocumentTagsIn(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("tags")
    @classmethod
    def _trim(cls, v):
        cleaned = [t.strip() for t in v if t and t.strip()]
        # 去重 + 每个最多 64 字符
        seen, out = set(), []
        for t in cleaned:
            short = t[:64]
            if short not in seen:
                seen.add(short)
                out.append(short)
        if len(out) > 32:
            raise ValueError("标签最多 32 个")
        return out


@router.put("/{library_id}/documents/{doc_id}/tags")
async def set_document_tags(library_id: int, doc_id: uuid.UUID, body: DocumentTagsIn,
                            s: AsyncSession = Depends(get_session)):
    """覆盖式更新文档标签。"""
    _, doc = await bound_document(s, library_id, doc_id, lock=True)
    doc.tags = body.tags
    await s.commit()
    await s.refresh(doc)
    return doc_out(doc)


@router.put("/{library_id}/documents/{doc_id}/config")
async def configure_document(library_id: int, doc_id: uuid.UUID, body: DocumentConfigIn,
                             s: AsyncSession = Depends(get_session)):
    """文档级索引设置：保存后下次解析生效（当前分段不变，需重新解析应用）。"""
    _, doc = await bound_document(s, library_id, doc_id, lock=True)
    doc.engine_config = {"processing": body.processing.model_dump(), "strategy": body.strategy,
                         "enhancements": body.enhancements, "type_rules": body.type_rules}
    await s.commit()
    await s.refresh(doc)
    return doc_out(doc)


@router.get("/{library_id}/documents/{doc_id}/original")
async def original(library_id: int, doc_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    _, doc = await bound_document(s, library_id, doc_id)
    if doc.status == "PENDING":
        raise HTTPException(409, "文档待同步，暂无原件")
    from kb_common.clients import minio_client
    from urllib.parse import quote
    def read():
        response = minio_client.minio.get_object(minio_client.RAW, doc.storage_path)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    return Response(await asyncio.to_thread(read), media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.name)}"})


@router.get("/{library_id}/documents/{doc_id}/preview")
async def preview_original(library_id: int, doc_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    """用 kkFileView 预览文档库原件。

    关键点：fullfilename 是 kkFileView 的查询参数（让它识别文件扩展名），
    不能拼到 MinIO presigned URL 里——presigned URL 签名对 query 敏感，
    追加任何 query 参数都会让 MinIO 返回 403。
    """
    import base64
    import urllib.parse
    from datetime import timedelta
    from kb_common.clients import minio_client
    _, doc = await bound_document(s, library_id, doc_id)
    if doc.status == "PENDING":
        raise HTTPException(409, "文档待同步，暂无原件")
    # presigned URL 必须原样保留（签名只覆盖原始 query）
    presigned = minio_client.minio.presigned_get_object(
        minio_client.RAW, doc.storage_path, expires=timedelta(hours=1))
    encoded = base64.b64encode(presigned.encode()).decode()
    kkfv_url = get_settings().kkfv_url.rstrip('/')
    # fullfilename 拼在 kkFileView 这一层，不进 base64
    return {"preview_url": f"{kkfv_url}/onlinePreview?url={urllib.parse.quote(encoded)}&fullfilename={urllib.parse.quote(doc.name)}",
            "filename": doc.name}


@router.get("/{library_id}/documents/{doc_id}/parsed-content")
async def parsed_content(library_id: int, doc_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    """解析原文（MinerU markdown / 纯文本直通）：JSON 内容标记 format=json 走 JSON 视图，
    其余 format=markdown 由前端渲染；图片相对引用 images/ 与分段渲染共用代理改写。"""
    _, doc = await bound_document(s, library_id, doc_id)
    from kb_common.clients import minio_client

    def read():
        response = minio_client.minio.get_object(minio_client.RAW, _parsed_markdown_key(doc))
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    try:
        data = await asyncio.to_thread(read)
    except Exception:
        raise HTTPException(404, "暂无解析原文，请重新解析后生成")
    text = data.decode("utf-8", errors="replace")
    return {"content": text, "format": _sniff_format(text)}


async def chunk_context(s, library_id, doc_id, writing=False):
    lib, doc = await bound_document(s, library_id, doc_id, lock=writing)
    if writing and doc.status == "PARSING":
        raise HTTPException(409, "解析期间不能修改分段")
    return lib, doc


async def _owned_chunk(s, doc, chunk_id: str):
    try:
        chunk_uuid = uuid.UUID(chunk_id)
    except ValueError:
        raise HTTPException(404, "分段不存在")
    chunk = (await s.execute(select(LibraryChunk).where(
        LibraryChunk.id == chunk_uuid, LibraryChunk.document_id == doc.id))).scalar_one_or_none()
    if not chunk:
        raise HTTPException(404, "分段不存在")
    return chunk


@router.get("/{library_id}/documents/{doc_id}/chunks")
async def chunks(library_id: int, doc_id: uuid.UUID, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), keywords: str = Query("", max_length=200), s: AsyncSession = Depends(get_session)):
    lib, doc = await chunk_context(s, library_id, doc_id)
    where = [LibraryChunk.document_id == doc.id, LibraryChunk.parent_id.is_(None)]
    if keywords.strip():
        where.append(LibraryChunk.content.contains(keywords.strip(), autoescape=True))
    total = (await s.execute(select(func.count()).select_from(LibraryChunk).where(*where))).scalar_one()
    rows = (await s.execute(select(LibraryChunk).where(*where)
        .order_by(LibraryChunk.position, LibraryChunk.created_at)
        .offset((page - 1) * size).limit(size))).scalars().all()
    # 子分段随父分段返回（父子分段：子块是检索单元，父块作召回上下文；前端在父卡片内展示）
    children_map: dict = {}
    if rows:
        child_rows = (await s.execute(select(LibraryChunk).where(
            LibraryChunk.document_id == doc.id,
            LibraryChunk.parent_id.in_([c.id for c in rows]))
            .order_by(LibraryChunk.parent_id, LibraryChunk.child_index.nulls_last(),
                      LibraryChunk.created_at))).scalars().all()
        for child in child_rows:
            children_map.setdefault(child.parent_id, []).append(chunk_out(child))
    return {"chunks": [chunk_out(c) | {"children": children_map.get(c.id, [])} for c in rows], "total": total}


@router.post("/{library_id}/documents/{doc_id}/chunks")
@router.put("/{library_id}/documents/{doc_id}/chunks/{chunk_id}")
async def write_chunk(library_id: int, doc_id: uuid.UUID, body: ChunkIn, chunk_id: str | None = None, s: AsyncSession = Depends(get_session)):
    lib, doc = await chunk_context(s, library_id, doc_id, writing=True)
    if chunk_id:
        chunk = await _owned_chunk(s, doc, chunk_id)
        chunk.content, chunk.available = body.content, body.available
        chunk.important_keywords = body.important_keywords
    else:
        if body.insert_before or body.insert_after:
            ref = await _owned_chunk(s, doc, body.insert_before or body.insert_after)
            position = ref.position if body.insert_before else ref.position + 1
            # 腾出插入位：该位置起整体后移（子分段与父分段同 position，随之一起移动）
            await s.execute(update(LibraryChunk)
                .where(LibraryChunk.document_id == doc.id, LibraryChunk.position >= position)
                .values(position=LibraryChunk.position + 1))
        else:
            position = (await s.execute(select(func.coalesce(func.max(LibraryChunk.position), -1))
                .where(LibraryChunk.document_id == doc.id, LibraryChunk.parent_id.is_(None)))).scalar_one() + 1
        chunk = LibraryChunk(document_id=doc.id, content=body.content, available=body.available,
                             important_keywords=body.important_keywords, position=position)
        s.add(chunk)
        await s.flush()
    doc.chunk_count = await parent_chunk_count(s, doc.id)
    await s.commit()
    return {"ok": True, "data": chunk_out(chunk)}


@router.delete("/{library_id}/documents/{doc_id}/chunks/{chunk_id}")
async def delete_chunk(library_id: int, doc_id: uuid.UUID, chunk_id: str, s: AsyncSession = Depends(get_session)):
    lib, doc = await chunk_context(s, library_id, doc_id, writing=True)
    chunk = await _owned_chunk(s, doc, chunk_id)
    await s.delete(chunk)  # 子分段由 FK ondelete CASCADE 一并清理
    doc.chunk_count = await parent_chunk_count(s, doc.id)
    await s.commit()
    return {"ok": True}


@router.delete("/{library_id}/documents/{doc_id}")
async def delete_document(library_id: int, doc_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    lib, doc = await bound_document(s, library_id, doc_id, lock=True)
    if doc.status == "PARSING":
        raise HTTPException(409, "请先停止解析再删除")
    # 级联取消批处理模式下关联该文档的排队同步任务（PENDING 文档删除后无需再同步）
    from datetime import datetime as _dt

    from kb_common.models import SyncTask
    from sqlalchemy import update as _sa_update
    await s.execute(_sa_update(SyncTask).where(
        SyncTask.library_document_id == str(doc.id),
        SyncTask.status == "pending",
    ).values(status="failed", finished_at=_dt.utcnow(), error="目标文档已删除，任务取消"))
    # Keep original object for disaster recovery; local catalog entry (and chunks) removed.
    await s.delete(doc)
    await s.commit()
    return {"ok": True}


@router.get("/{library_id}/export")
async def export_library(library_id: int, s: AsyncSession = Depends(get_session)):
    """Portable archive for connector removal; original files are project-owned.

    All content is serialized from local tables (originals in MinIO + LibraryChunk
    rows); no remote engine calls. Reject a running parse rather than silently
    exporting an incomplete chunk set.
    """
    import json
    import tempfile
    import zipfile
    from starlette.background import BackgroundTask
    from fastapi.responses import FileResponse
    from kb_common.clients import minio_client

    lib = await library(s, library_id, lock=True)
    docs = (await s.execute(select(LibraryDocument).where(LibraryDocument.library_id == library_id))).scalars().all()
    manifest = {"schema_version": 1, "name": lib.name, "description": lib.description,
                "processing": lib.engine_config.get("processing"), "documents": []}
    temp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    path = temp.name
    temp.close()
    import os
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for doc in docs:
                if doc.status == "PARSING":
                    raise HTTPException(409, "文档正在解析，请完成后再导出")
                if doc.status == "PENDING":
                    raise HTTPException(409, "有文档待同步，请同步完成后再导出")
                original_path = f"originals/{doc.id}/{doc.name}"
                def archive_original():
                    response = minio_client.minio.get_object(minio_client.RAW, doc.storage_path)
                    try:
                        with archive.open(original_path, "w") as output:
                            for block in response.stream(1024 * 1024):
                                output.write(block)
                    finally:
                        response.close()
                        response.release_conn()
                await asyncio.to_thread(archive_original)
                rows = (await s.execute(select(LibraryChunk).where(LibraryChunk.document_id == doc.id)
                    .order_by(LibraryChunk.parent_id.is_not(None), LibraryChunk.position,
                              LibraryChunk.created_at))).scalars().all()
                archive.writestr(f"chunks/{doc.id}.json",
                                 json.dumps([chunk_out(c) for c in rows], ensure_ascii=False))
                manifest["documents"].append({"id": str(doc.id), "name": doc.name, "status": doc.status,
                    "original": original_path, "chunk_count": doc.chunk_count, "chunks": f"chunks/{doc.id}.json"})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return FileResponse(path, media_type="application/zip", filename=f"library-{library_id}.zip",
                            background=BackgroundTask(os.unlink, path))
    except Exception:
        os.unlink(path)
        raise


@router.delete("/{library_id}", dependencies=[Depends(require_role("super_admin", "admin"))])
async def delete_library(library_id: int, s: AsyncSession = Depends(get_session)):
    lib = await library(s, library_id, lock=True)
    count = (await s.execute(select(func.count()).select_from(LibraryDocument).where(LibraryDocument.library_id == library_id))).scalar_one()
    if lib.enabled or count:
        raise HTTPException(409, "请先导出归档、停用知识库并清空文档，再删除空库")
    await s.delete(lib)
    await s.commit()
    return {"ok": True}
